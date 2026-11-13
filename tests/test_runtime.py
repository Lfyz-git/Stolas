"""Current installation transactions and exact resource ownership checks."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/manage.py").is_file()
if HAS_TOOLS:
    from tools import deploy, install, layout, manage, resources


class DockerFixture:
    """Stateful fake daemon: removal tests observe actual ownership decisions."""
    def __init__(self):
        self.items, self.volumes, self.images, self.calls = [], {}, {}, []

    def container(self, root, project, instance=None, volume=None, id=None, version="0.6.6"):
        labels = {"com.docker.compose.project": project, "com.docker.compose.project.working_dir": str(root), "com.docker.compose.service": "stolas"}
        if instance:
            labels["org.stolas.instance"] = instance
        item = {"id": id or project + "-id", "name": "/" + project, "image": project + ":" + version,
                "image_id": "sha256:" + project + version, "running": True, "labels": labels,
                "mounts": [{"Type": "volume", "Name": volume, "Destination": "/data"}] if volume else []}
        self.items.append(item)
        self.images[item["image"]] = {"id": item["image_id"], "labels": {"org.stolas.instance": instance} if instance else {}}
        return item

    def __call__(self, args, **kwargs):
        args = args[args.index("docker") + 1:]
        self.calls.append(args)
        value, code = "", 0
        if args[:2] == ["ps", "-aq"]:
            items = self.items
            if "--filter" in args:
                name = args[args.index("--filter") + 1].removeprefix("volume=")
                items = [c for c in items if any(m.get("Name") == name for m in c["mounts"])]
            value = "\n".join(c["id"] for c in items)
        elif args[0] == "inspect":
            value = "\n".join(json.dumps(c) for c in self.items if c["id"] in args[3:])
        elif args[:2] == ["volume", "inspect"]:
            value, code = (json.dumps(self.volumes[args[-1]]), 0) if args[-1] in self.volumes else ("", 1)
        elif args[:2] == ["image", "inspect"]:
            value, code = (json.dumps(self.images[args[-1]]), 0) if args[-1] in self.images else ("", 1)
        elif args[:2] == ["volume", "create"]:
            labels = dict(args[index + 1].split("=", 1) for index, part in enumerate(args) if part == "--label")
            self.volumes[args[-1]] = {"name": args[-1], "labels": labels, "created": "new-fixture"}
            value = args[-1]
        elif args[0] == "stop":
            for c in self.items:
                if c["id"] == args[-1]:
                    c["running"] = False
        elif args[0] == "rm":
            self.items = [c for c in self.items if c["id"] != args[-1]]
        elif args[:2] == ["volume", "rm"]:
            self.volumes.pop(args[-1])
        elif args[:2] == ["image", "rm"]:
            self.images.pop(args[-1])
        elif args[0] == "compose":
            value = json.dumps({"volumes": {"stolas-data": {"name": "stolas-133eaddd33_stolas-data"}}})
        elif args[0] == "info":
            value = "linux"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, value, "")


@unittest.skipUnless(HAS_TOOLS, "Deployment tools are not part of the Core image")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "installation"
        self.root.mkdir()
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.docker = DockerFixture()
        docker_access = patch.object(install, "require_docker_access")
        docker_access.start()
        self.addCleanup(docker_access.stop)
        self.mock = patch.object(resources.environment, "command", side_effect=self.docker)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.command = patch.object(resources, "command", return_value=["docker"])
        self.command.start()
        self.addCleanup(self.command.stop)

    def runtime(self):
        deploy.stage_sources(ROOT, self.root, "v0.6.6", "fixture")
        (self.root / ".env").write_text("STOLAS_API_TOKEN=" + "a" * 40 + "\nSTOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\n")
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/local.json").write_text(json.dumps(install.DEFAULT))

    def activate(self):
        resources.configure(self.root, ["docker"])
        resources.activate(self.root, ["docker"])
        env = resources.read_env(self.root)
        self.docker.container(self.root, env["STOLAS_PROJECT_NAME"], env["STOLAS_INSTANCE_ID"], env["STOLAS_DATA_VOLUME"], id="replacement-id")
        resources.complete(self.root, ["docker"])
        return 0

    def test_failed_update_restores_current_files_and_service(self):
        self.runtime(); self.activate()
        old_env = (self.root / ".env").read_bytes()
        code = self.root / ".stolas/build/agent/__init__.py"
        original_code = code.read_bytes() + b"\n# Current installation snapshot\n"
        code.write_bytes(original_code)
        self.docker.items[0]["id"] = "original-id"
        def fail(*a, **kw):
            self.assertNotEqual(code.read_bytes(), original_code)
            install.write_private(self.root / ".env", (self.root / ".env").read_text().replace("8080", "8081"))
            self.activate()
            return 1
        def restart(root, docker):
            self.assertEqual((root / ".env").read_bytes(), old_env)
            self.assertEqual(code.read_bytes(), original_code)
            env = resources.read_env(root)
            self.docker.container(root, env["STOLAS_PROJECT_NAME"], env["STOLAS_INSTANCE_ID"], env["STOLAS_DATA_VOLUME"], id="restored-id")
        with patch.object(deploy, "run_installer", side_effect=fail), patch.object(resources, "restart", side_effect=restart) as recovered:
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 1)
        recovered.assert_called_once()
        self.assertEqual((self.root / ".env").read_bytes(), old_env)
        self.assertEqual(code.read_bytes(), original_code)
        self.assertEqual([c["id"] for c in self.docker.items], ["restored-id"])
        self.assertEqual(deploy.read_json(self.root / ".stolas/state/transaction.json")["phase"], "rolled_back")

    def test_interrupt_then_retry_recovers_before_current_update(self):
        self.runtime(); self.activate()
        self.docker.items[0]["id"] = "original-id"
        def restart(root, docker):
            env = resources.read_env(root)
            self.docker.container(root, env["STOLAS_PROJECT_NAME"], env["STOLAS_INSTANCE_ID"], env["STOLAS_DATA_VOLUME"], id="restored-id")
        with patch.object(deploy, "run_installer", side_effect=lambda *a, **k: (self.activate(), 130)[1]):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 130)
        with patch.object(resources, "restart", side_effect=restart) as recovered, patch.object(deploy, "run_installer", side_effect=lambda *a, **k: self.activate()):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 0)
        recovered.assert_called_once()
        self.assertEqual(len(self.docker.items), 1)

    def test_copy_failure_does_not_change_current_installation(self):
        self.runtime()
        before = {name: (self.root / name).read_bytes() for name in (".env", "config/local.json", ".stolas/build/agent/runner.py", ".stolas/state/managed.json")}
        original = deploy.replace_file
        count = 0
        def fail(source, destination):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("injected write failure")
            return original(source, destination)
        with patch.object(deploy, "replace_file", side_effect=fail), self.assertRaises(OSError):
            deploy.stage_sources(ROOT, self.root, "v0.6.6", "")
        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content, name)

    @unittest.skipIf(os.name == "nt", "Purge unlinks a held POSIX lock; supported platform is Linux")
    def test_purge_removes_exact_files_and_keeps_foreign_files(self):
        self.runtime(); self.activate()
        (self.root / "notes.txt").write_text("foreign")
        (self.root / ".stolas/user-note.txt").write_text("foreign internal")
        foreign = self.docker.container(self.root.parent / "n8n", "n8n", "foreign")
        with patch("builtins.input", side_effect=["УДАЛИТЬ"]):
            self.assertEqual(manage.uninstall(self.root, purge=True), 0)
        self.assertEqual(self.docker.items, [foreign])
        self.assertFalse(self.docker.volumes)
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / "stolas").exists())
        self.assertEqual((self.root / "notes.txt").read_text(), "foreign")
        self.assertEqual((self.root / ".stolas/user-note.txt").read_text(), "foreign internal")

    def test_shared_volume_is_never_purged(self):
        self.runtime(); self.activate()
        name = resources.read_env(self.root)["STOLAS_DATA_VOLUME"]
        foreign = self.docker.container(self.root.parent / "foreign", "foreign", "different", volume=name)
        with patch("builtins.input", side_effect=["УДАЛИТЬ"]), self.assertRaisesRegex(RuntimeError, "другим контейнером"):
            manage.uninstall(self.root, purge=True)
        self.assertIn(name, self.docker.volumes)
        self.assertIn(foreign, self.docker.items)

    def test_shared_image_is_preserved(self):
        self.runtime(); self.activate()
        foreign = self.docker.container(self.root.parent / "foreign", "foreign", "different")
        foreign["image_id"] = self.docker.images["stolas:0.6.6"]["id"]
        with patch("builtins.input", side_effect=["1", "1"]):
            manage.uninstall(self.root)
        self.assertIn("stolas:0.6.6", self.docker.images)
        self.assertIn(foreign, self.docker.items)

    def test_replaced_volume_with_same_name_is_not_owned(self):
        self.runtime(); self.activate()
        name = resources.read_env(self.root)["STOLAS_DATA_VOLUME"]
        self.docker.volumes[name]["created"] = "replaced"
        self.docker.volumes[name]["labels"] = {}
        with self.assertRaisesRegex(RuntimeError, "Принадлежность"):
            manage.removal_plan(self.root, ["docker"])


    @unittest.skipIf(os.name == "nt", "POSIX symlinks and modes")
    def test_private_modes_and_symlink_refusal(self):
        self.runtime()
        install.write_private(self.root / ".env", "private\n")
        self.assertEqual((self.root / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / ".stolas/state").stat().st_mode & 0o777, 0o700)
        (self.root / ".stolas/user-link").symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            layout.bounded(self.root, ".stolas/user-link/file")


@unittest.skipUnless(HAS_TOOLS, "Installer sources are not in the production image")
class InstallerLockTests(unittest.TestCase):
    def setUp(self):
        docker_access = patch.object(install, "require_docker_access")
        docker_access.start()
        self.addCleanup(docker_access.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("stolas", "install.sh", "tools/install.py", "tools/deploy.py", "agent/config.py", "compose.yaml"):
            path = self.source / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("old")
        self.target = self.root / "stolas"

    def run_deploy(self, action=None):
        with patch.object(deploy, "run_installer", return_value=0):
            return deploy.deploy(self.source, self.target, action=action)

    @unittest.skipIf(os.name == "nt", "POSIX flock/symlinks")
    def test_competing_installers_and_symlinks(self):
        with deploy.install_lock(self.target):
            with self.assertRaises(RuntimeError):
                self.run_deploy()
        link = self.root / "link"
        link.symlink_to(self.target, target_is_directory=True)
        with self.assertRaises(ValueError):
            deploy.safe_path(link)


if __name__ == "__main__":
    unittest.main()
