"""Real Linux Docker: current installation, SQLite, health, rollback and offline purge."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/manage.py").is_file()
if HAS_TOOLS:
    from tools import deploy, entrypoints, install, layout, manage, resources


@unittest.skipUnless(HAS_TOOLS and sys.platform.startswith("linux") and os.getenv("STOLAS_RUNTIME_DOCKER") == "1", "Opt-in current runtime transactions on Linux Docker")
class RuntimeDockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stolas-runtime-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "installation"
        self.root.mkdir()
        self.suffix = uuid.uuid4().hex[:10]
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

    def installer(self, target, configure_only=False, reuse=False, recover=False, skip_first_test=False):
        args = [sys.executable, str(layout.code_root(target) / "tools/install.py"), "--root", str(target)]
        if configure_only:
            args.append("--configure-only")
        if reuse:
            args.append("--reuse-config")
        if recover:
            args.append("--recover")
        if skip_first_test:
            args.append("--skip-first-test")
        result = subprocess.run(args, input="apply\nlater\n", text=True, capture_output=True, env=install.clean_env(), timeout=240)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if not configure_only:
            self.assert_api_ready(target)
        self.assertNotIn("Тест скорости (", result.stdout)
        self.owned_images.add("stolas:" + resources.core_version(target))
        env = resources.read_env(target)
        if env.get("STOLAS_DATA_VOLUME"):
            self.owned_volumes.add(env["STOLAS_DATA_VOLUME"])
        return result.returncode

    def assert_api_ready(self, target):
        env = resources.read_env(target)
        response = install.request_json("http://127.0.0.1:" + env["STOLAS_PORT"],
                                        "/healthz", env["STOLAS_API_TOKEN"])
        self.assertEqual(response, {"status": "ready"})

    def api_latest(self, token=None):
        env = resources.read_env(self.root)
        request = urllib.request.Request("http://127.0.0.1:" + env["STOLAS_PORT"] + "/v1/results/latest", headers={"Authorization": "Bearer " + (token if token is not None else env["STOLAS_API_TOKEN"])})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            return json.load(response)

    def running_config(self):
        result = self.docker("exec", "stolas", "python3", "-c",
                             "import os; print(open(os.environ['STOLAS_CONFIG']).read())")
        return json.loads(result.stdout)

    def assert_neighbour(self):
        self.assertEqual(self.docker("inspect", "--format", "{{.State.Running}}", self.foreign).stdout.strip(), "true")

    def current_installation(self):
        initial = deploy.stage_sources(ROOT, self.root, "v0.6.6", "fixture")
        # A distinguishable current snapshot proves that rollback restores files,
        # even when the candidate is another revision of the same Core version.
        compose = self.root / "compose.yaml"
        compose.write_text(compose.read_text() + "\n# Current installation snapshot\n")
        (self.root / "config").mkdir(exist_ok=True)
        cfg = json.loads((ROOT / "config/example.json").read_text())
        cfg["route"]["mode"] = "off"
        cfg["node"] = "current-installation-fixture"
        (self.root / "config/local.json").write_text(json.dumps(cfg))
        (self.root / ".env").write_text("STOLAS_API_TOKEN=" + "b" * 40 + "\nSTOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=" + str(self.port) + "\n")
        self.installer(self.root, reuse=True)
        deploy.finalize(self.root, initial)
        deploy.mark_backup(initial, "complete")
        deploy.atomic_json(self.root / ".stolas/state/transaction.json",
                           {"phase": "complete", "backup": initial.relative_to(self.root).as_posix(), "configured": False})

    def test_current_update_failure_rollback_repeat_and_purge(self):
        self.current_installation()
        self.volume = resources.read_env(self.root)["STOLAS_DATA_VOLUME"]
        old_compose = resources.compose(self.root, ["docker"])
        sample = {"id": "retained-sqlite-row", "started_epoch": 1, "time": "2026-10-10T00:00:00Z", "node": "fixture", "status": "ok", "primary": None, "confirmation": None, "attempts": [], "errors": []}
        seed = "import json,sys; from agent.storage import Store; Store('/data').save(json.load(sys.stdin),100)"
        subprocess.run(old_compose + ["exec", "-T", "stolas", "python3", "-c", seed], input=json.dumps(sample), text=True, env=install.clean_env(), check=True, timeout=15)
        before = resources.volume_info(self.root, ["docker"], self.volume)
        self.assertEqual(self.api_latest(), sample)
        original_env = (self.root / ".env").read_bytes()
        original_config = (self.root / "config/local.json").read_bytes()
        original_compose = (self.root / "compose.yaml").read_bytes()
        old_id = self.docker("inspect", "--format", "{{.Id}}", "stolas").stdout.strip()
        def fail_after_handover(*args, **kwargs):
            cfg = json.loads(original_config)
            cfg["node"] = "failed-candidate-fixture"
            (self.root / "config/local.json").write_text(json.dumps(cfg))
            install.write_private(self.root / ".env", (self.root / ".env").read_text().replace("b" * 40, "c" * 40))
            self.installer(*args, **kwargs)
            self.assertNotEqual(self.docker("inspect", "--format", "{{.Id}}", "stolas").stdout.strip(), old_id)
            self.assertNotEqual((self.root / "compose.yaml").read_bytes(), original_compose)
            self.assertEqual(self.running_config()["node"], "failed-candidate-fixture")
            self.assertEqual(self.api_latest(), sample)
            return 1
        with patch.object(deploy, "run_installer", side_effect=fail_after_handover):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 1)
        self.assertEqual((self.root / ".env").read_bytes(), original_env)
        self.assertEqual((self.root / "config/local.json").read_bytes(), original_config)
        self.assertEqual((self.root / "compose.yaml").read_bytes(), original_compose)
        self.assertEqual(self.running_config(), json.loads(original_config))
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.api_latest(token="c" * 40)
        self.assertEqual(rejected.exception.code, 401)
        rejected.exception.close()
        journal = deploy.read_json(self.root / ".stolas/state/transaction.json")
        self.assertEqual(journal["phase"], "rolled_back")
        backup = self.root / journal["backup"]
        self.assertEqual(deploy.backup_state(self.root, backup)["retention_status"], "rolled_back")
        for name, content in ((".env", original_env), ("config/local.json", original_config), ("compose.yaml", original_compose)):
            self.assertEqual((backup / "files" / name).read_bytes(), content)
        self.assertEqual(self.api_latest(), sample)
        self.assert_neighbour()
        with patch.object(deploy, "run_installer", side_effect=self.installer):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 0)
        self.assertEqual((self.root / "config/local.json").read_bytes(), original_config)
        self.assertEqual((self.root / ".env").read_bytes(), original_env)
        self.assertEqual((self.root / "compose.yaml").read_bytes(), (ROOT / "compose.yaml").read_bytes())
        journal = deploy.read_json(self.root / ".stolas/state/transaction.json")
        self.assertEqual(journal["phase"], "complete")
        self.assertEqual(deploy.backup_state(self.root, self.root / journal["backup"])["retention_status"], "complete")
        self.assertEqual(resources.read_env(self.root)["STOLAS_PROJECT_NAME"], "stolas")
        self.assertEqual(resources.read_env(self.root)["STOLAS_DATA_VOLUME"], self.volume)
        self.assertEqual(resources.volume_info(self.root, ["docker"], self.volume), before)
        self.assertEqual(self.api_latest(), sample)
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
        initial = deploy.stage_sources(ROOT, self.root, "v0.6.6", "fixture")
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
        deploy.finalize(self.root, initial)
        deploy.mark_backup(initial, "complete")
        deploy.atomic_json(self.root / ".stolas/state/transaction.json",
                           {"phase": "complete", "backup": initial.relative_to(self.root).as_posix(), "configured": False})
        # Startup and recovery must leave history empty until an explicit run.
        for recover in (False, True):
            if recover:
                self.installer(self.root, recover=True)
            with self.assertRaises(urllib.error.HTTPError) as missing:
                self.api_latest()
            self.assertEqual(missing.exception.code, 404)
            with missing.exception as response:
                self.assertEqual(json.load(response), {"error": "no_results"})
        env = resources.read_env(self.root)
        volume = env["STOLAS_DATA_VOLUME"]
        before = resources.volume_info(self.root, ["docker"], volume)
        def cli(*args, answer=""):
            return subprocess.run(["sh", str(self.root / "stolas"), *args], input=answer,
                                  capture_output=True, text=True, timeout=150, env=install.clean_env())
        measured = cli("run", "--json")
        self.assertEqual(measured.returncode, 0, measured.stdout + measured.stderr)
        sample = json.loads(measured.stdout)
        self.assertEqual(sample["status"], "ok")
        self.assertEqual(self.api_latest(), sample)
        def configure(node):
            cfg.update(node=node, min_interval=86400)
            install.save_draft(self.root, {"config": cfg, "api": install.read_api(self.root), "completed": []})
            result = cli("configure", answer="resume\napply\nlater\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_api_ready(self.root)
            self.assertNotIn("Тест скорости (", result.stdout)
            self.assertEqual(install.validated(self.root / "config/local.json")["node"], node)
            self.assertEqual(deploy.read_json(self.root / ".stolas/state/transaction.json")["phase"], "complete")
            self.assertEqual(self.api_latest(), sample)
            self.assertEqual(resources.volume_info(self.root, ["docker"], volume), before)
        configure("cooldown-fixture")
        blocked = cli("run", "--json")
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertEqual(json.loads(blocked.stderr.splitlines()[-1])["reason"], "Cooldown")
        configure("changed-during-cooldown")
        locker = "stolas-test-lock-" + self.suffix
        self.addCleanup(lambda: self.docker("rm", "-f", locker, check=False))
        code = "import fcntl,time; f=open('/data/run.lock','a+b'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(600)"
        self.docker("run", "-d", "--name", locker, "-v", volume + ":/data", "--entrypoint", "python3", "stolas:ci", "-c", code)
        for _ in range(100):
            if "ready" in self.docker("logs", locker).stdout:
                break
            time.sleep(.05)
        else:
            self.fail("Measurement lock fixture did not start")
        blocked = cli("run", "--json")
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertEqual(json.loads(blocked.stderr.splitlines()[-1])["reason"], "Busy")
        configure("changed-during-measurement")
        self.assertEqual(self.docker("inspect", "--format", "{{.State.Running}}", locker).stdout.strip(), "true")
        self.docker("rm", "-f", locker)
        configure("repeat-retention-fixture")
        backups = list((self.root / deploy.BACKUP_ROOT).iterdir())
        self.assertEqual(len(backups), 3)
        self.assertTrue(all(deploy.backup_state(self.root, p)["retention_status"] == "complete" for p in backups))
        self.assertEqual({p.name for p in self.root.iterdir()}, {"stolas", "compose.yaml", ".env", "config", ".stolas"})
        result = subprocess.run(["sh", str(self.root / "stolas"), "diagnose", "--json"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("a" * 40, result.stdout)
        self.assertTrue(json.loads(result.stdout)["docker"]["available"])
        self.assert_neighbour()

    def test_current_update_path_rollback_history_and_offline_uninstall_purge(self):
        self.current_installation()
        token = resources.read_env(self.root)["STOLAS_API_TOKEN"]
        original_config = (self.root / "config/local.json").read_bytes()
        original_compose = (self.root / "compose.yaml").read_bytes()
        sample = {"id": "current-path-retained-row", "started_epoch": 1, "time": "2026-10-10T00:00:00Z", "node": "fixture", "status": "ok", "primary": None, "confirmation": None, "attempts": [], "errors": []}
        compose = resources.compose(self.root, ["docker"])
        subprocess.run(compose + ["exec", "-T", "stolas", "python3", "-c", "import json,sys; from agent.storage import Store; Store('/data').save(json.load(sys.stdin),100)"], input=json.dumps(sample), text=True, check=True, timeout=15, env=install.clean_env())
        volume = resources.read_env(self.root)["STOLAS_DATA_VOLUME"]
        before = resources.volume_info(self.root, ["docker"], volume)
        # Exercise the complete updater and installer as root, then all management
        # operations as the original user. Never touch the host's real installs.
        result = subprocess.run(["sudo", "-n", sys.executable, str(ROOT / "tools/deploy.py"), "--target", str(self.root), "--source-ref", "v0.6.6", "--action", "update"], input="apply\nlater\n", text=True, capture_output=True, cwd=ROOT, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Stolas успешно обновлён до v0.6.6", " ".join(result.stdout.split()))
        self.assertLess(result.stdout.index("успешно обновлён"), result.stdout.index("Точки восстановления"))
        self.owned_images.add("stolas:0.6.6")
        for path in self.root.rglob("*"):
            self.assertEqual(path.stat().st_uid, os.getuid(), str(path))
        journal = deploy.read_json(self.root / ".stolas/state/transaction.json")
        previous = self.root / journal["backup"]
        self.assertEqual(journal["phase"], "complete")
        self.assertEqual(deploy.backup_state(self.root, previous)["retention_status"], "complete")
        self.assertEqual((previous / "files/compose.yaml").read_bytes(), original_compose)
        self.assertNotEqual((self.root / "compose.yaml").read_bytes(), original_compose)
        binary = Path(self.tmp.name) / "commands"
        wrapper = Path(entrypoints.install(self.root, binary)["path"])
        with patch.object(deploy, "run_installer", side_effect=self.installer):
            self.assertEqual(deploy.deploy(ROOT, self.root, ref="v0.6.6", action="update"), 0)
        # Keep the first update's restore point across a repeated update.
        self.assertTrue(previous.is_dir())
        env = {**install.clean_env(), "PATH": str(binary) + os.pathsep + os.environ["PATH"]}
        def cli(args, answer=""):
            return subprocess.run(["stolas", *args], cwd=self.tmp.name, env=env, input=answer, text=True, capture_output=True, timeout=150)
        result = cli(["version"])
        self.assertEqual((result.returncode, result.stdout), (0, "Stolas Core v0.6.6\n"))
        # Ordinary configure applies and checks the service without a fabricated
        # completed-measurement checkpoint or external speed-test traffic.
        cfg = json.loads(original_config)
        cfg["node"] = "updated-path-fixture"
        install.save_draft(self.root, {"config": cfg, "api": install.read_api(self.root), "completed": []})
        result = cli(["configure"], "resume\napply\nlater\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(install.validated(self.root / "config/local.json")["node"], "updated-path-fixture")
        result = cli(["diagnose", "--json"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(p["owner_mismatch"] for p in json.loads(result.stdout)["permissions"]["paths"]))
        deploy.atomic_json(self.root / ".stolas/state/transaction.json", {"phase": "complete", "backup": previous.relative_to(self.root).as_posix(), "configured": True})
        result = cli(["rollback"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "config/local.json").read_bytes(), original_config)
        self.assertEqual((self.root / "compose.yaml").read_bytes(), original_compose)
        self.assertEqual(self.running_config(), json.loads(original_config))
        self.assertEqual(resources.core_version(self.root), "0.6.6")
        result = cli(["version"])
        self.assertEqual((result.returncode, result.stdout), (0, "Stolas Core v0.6.6\n"))
        result = cli(["history"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(sample["id"], result.stdout)
        self.assertEqual(self.api_latest(), sample)
        self.assertEqual(resources.read_env(self.root)["STOLAS_API_TOKEN"], token)
        self.assertEqual(resources.volume_info(self.root, ["docker"], volume), before)
        result = cli(["uninstall"], "remove\nconfirm\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(wrapper.exists())
        self.assertIsNotNone(resources.volume_info(self.root, ["docker"], volume))
        self.assertIsNone(resources.image_info(self.root, ["docker"], "stolas:0.6.6"))
        self.assert_neighbour()
        wrapper = Path(entrypoints.install(self.root, binary)["path"])
        result = cli(["uninstall", "--purge"], "УДАЛИТЬ\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(wrapper.exists())
        self.assertIsNone(resources.volume_info(self.root, ["docker"], volume))
        self.assert_neighbour()


@unittest.skipUnless(HAS_TOOLS and sys.platform.startswith("linux") and os.getenv("STOLAS_RUNTIME_DOCKER") == "1", "Opt-in Docker PID namespace and healthcheck regression")
class ContainerProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stolas-process-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o755)
        self.name = "stolas-process-test-" + uuid.uuid4().hex[:10]
        self.addCleanup(lambda: self.docker("rm", "-f", self.name, check=False))
        self.mode = self.root / "health-mode"
        self.mode.write_text("healthy")
        # Delay only the image's real healthcheck, not Core or inspection commands.
        (self.root / "sitecustomize.py").write_text("""import os,sys,time
from pathlib import Path
command = sys.orig_argv[-1]
if '/healthz' in command:
    mode = Path('/fixture/health-mode').read_text()
    if mode == 'timeout':
        time.sleep(4)
    elif mode == 'error':
        os._exit(7)
if 'worker_main' in command and Path('/fixture/stall-worker').exists():
    import socket
    socket.gethostbyname = lambda host: time.sleep(30)
""")
        config = json.loads((ROOT / "config/example.json").read_text())
        config["route"]["mode"] = "off"
        config.update(min_interval=0, retry_delay=0)
        config["server_groups"]["primary"]["servers"] = [dict(id="local", host="127.0.0.1", ports=[5201], min_download_mbps=0, min_upload_mbps=0)]
        for group in ("additional", "emergency"):
            config["server_groups"][group]["servers"] = []
        (self.root / "config.json").write_text(json.dumps(config))
        (self.root / "data").mkdir(mode=0o777)
        (self.root / "data").chmod(0o777)  # Synthetic data, written by image UID 10001.

    def docker(self, *args, check=True):
        return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=30, env=install.clean_env())

    def state(self):
        return json.loads(self.docker("inspect", self.name).stdout)[0]["State"]

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.1)
        logs = self.docker("logs", self.name)
        self.fail("Container did not reach the expected state: " + json.dumps(self.state()) + logs.stdout + logs.stderr)

    def start(self):
        self.docker("run", "-d", "--name", self.name, "--init=false", "--network", "none", "--read-only", "--tmpfs", "/tmp",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--mount", f"type=bind,src={self.root},dst=/fixture,readonly",
                    "--mount", f"type=bind,src={self.root / 'data'},dst=/data",
                    "-e", "PYTHONPATH=/fixture", "-e", "STOLAS_CONFIG=/fixture/config.json",
                    "-e", "STOLAS_API_TOKEN=" + "a" * 40,
                    "--health-interval", "300ms", "--health-timeout", "2s", "--health-retries", "1", "stolas:ci")
        self.wait_for(lambda: self.state()["Health"]["Status"] == "healthy")

    def processes(self):
        script = """import json,pathlib
rows=[]
for path in pathlib.Path('/proc').glob('[0-9]*/stat'):
    try:
        stat=path.read_text(); fields=stat[stat.rfind(')')+2:].split()
        rows.append(dict(pid=int(path.parent.name), state=fields[0], ppid=int(fields[1]),
                         name=stat[stat.find('(')+1:stat.rfind(')')],
                         probe=b'worker_main' in (path.parent/'cmdline').read_bytes()))
    except (OSError,ValueError):
        pass
print(json.dumps(rows))
"""
        return json.loads(self.docker("exec", self.name, "python3", "-c", script).stdout)

    def test_healthcheck_timeouts_and_errors_do_not_accumulate_orphans(self):
        self.start()
        self.assertTrue(any(p["pid"] == 1 and p["name"] == "python3" for p in self.processes()))
        for _ in range(2):
            for mode in ("error", "timeout"):
                self.mode.write_text(mode)
                self.wait_for(lambda: self.state()["Health"]["FailingStreak"] >= 3)
                self.assertEqual(self.state()["Health"]["Status"], "unhealthy")
                if mode == "timeout":
                    self.assertTrue(any("exceeded timeout" in log["Output"] for log in self.state()["Health"]["Log"]))
                self.mode.write_text("healthy")
                self.wait_for(lambda: self.state()["Health"]["Status"] == "healthy")
                # Give any orphan left by the old shell-form check time to exit.
                time.sleep(4.1)
                processes = self.processes()
                children = [p for p in processes if p["ppid"] == 1 or p["state"] == "Z"]
                self.assertEqual(children, [], "Healthcheck left adopted or zombie processes under Core PID 1")
                # Core, this inspection, and at most one active healthcheck.
                self.assertLessEqual(sum(p["name"] == "python3" for p in processes), 3)

    def assert_sigterm(self, active):
        self.start()
        sample = dict(id="preserved-before-sigterm", started_epoch=1, status="ok")
        self.docker("exec", self.name, "python3", "-c",
                    "from agent.storage import Store; Store('/data').save(" + repr(sample) + ",100)")
        before = (self.root / "config.json").read_bytes()
        if active:
            (self.root / "stall-worker").touch()
            script = "import urllib.request; r=urllib.request.Request('http://127.0.0.1:8080/v1/tests',method='POST',headers={'Authorization':'Bearer '+ 'a'*40}); urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=15).read()"
            self.docker("exec", "-d", self.name, "python3", "-c", script)
            self.wait_for(lambda: any(p["ppid"] == 1 and p["probe"] for p in self.processes()))
        started = time.monotonic()
        self.docker("stop", "--timeout", "5", self.name)
        self.assertLess(time.monotonic() - started, 5)
        state = self.state()
        self.assertFalse(state["Running"])
        self.assertEqual(state["ExitCode"], 0)  # Not Docker's SIGKILL fallback (137).
        logs = self.docker("logs", self.name)
        self.assertIn('"event":"service_stopped"', (logs.stdout + logs.stderr).replace(" ", ""))
        self.assertEqual((self.root / "config.json").read_bytes(), before)
        import sqlite3
        with sqlite3.connect(f"file:{self.root / 'data/history.sqlite3'}?mode=ro", uri=True) as db:
            self.assertEqual([json.loads(row[0]) for row in db.execute("SELECT body FROM results")], [sample])

    def test_sigterm_stops_idle_core_cleanly(self):
        self.assert_sigterm(active=False)

    def test_sigterm_during_a_probe_stops_core_and_preserves_sqlite(self):
        self.assert_sigterm(active=True)
