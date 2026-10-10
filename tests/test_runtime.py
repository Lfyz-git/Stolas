"""Migration fixtures and exact ownership checks. Docker execution is opt-in."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/manage.py").is_file()
if HAS_TOOLS:
    from tools import deploy, install, layout, manage, resources


def frozen_install(root):
    with tarfile.open(ROOT / "tests/fixtures/v0.4.0.tar.gz") as archive:
        names = []
        for member in archive:
            if member.isfile():
                name = member.name.split("/", 1)[1]
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
                names.append(name)
    (root / ".stolas-managed.json").write_text(json.dumps({"files": names, "ref": "v0.4.0"}))
    (root / ".env").write_text("STOLAS_API_TOKEN=" + "a" * 40 + "\nSTOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\nSTOLAS_PROJECT_NAME=stolas-133eaddd33\n")
    cfg = copy.deepcopy(install.DEFAULT)
    cfg["route"]["mode"] = "off"
    (root / "config/local.json").write_text(json.dumps(cfg))
    (root / ".stolas-progress.json").write_text(json.dumps({"stage": "complete", "first_status": "route_blocked"}))
    return root


class DockerFixture:
    """Stateful fake daemon: removal tests observe actual ownership decisions."""
    def __init__(self):
        self.items, self.volumes, self.images, self.calls = [], {}, {}, []

    def container(self, root, project, instance=None, volume=None, id=None, version="0.5.0"):
        labels = {"com.docker.compose.project": project, "com.docker.compose.project.working_dir": str(root), "com.docker.compose.service": "stolas"}
        if instance:
            labels["org.stolas.instance"] = instance
        item = {"id": id or project + "-id", "name": "/" + project, "image": project + ":" + version,
                "image_id": "sha256:" + project + version, "running": True, "labels": labels,
                "mounts": [{"Type": "volume", "Name": volume, "Destination": "/data"}] if volume else []}
        self.items.append(item)
        self.images[item["image"]] = {"id": item["image_id"], "labels": {"org.stolas.instance": instance} if instance else {}}
        return item

    def legacy(self, root):
        name = "stolas-133eaddd33_stolas-data"
        self.volumes[name] = {"name": name, "labels": {"com.docker.compose.project": "stolas-133eaddd33", "com.docker.compose.volume": "stolas-data"}, "created": "fixture-original"}
        return self.container(root, "stolas-133eaddd33", volume=name, id="old-id", version="0.4.0")

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
        self.mock = patch.object(resources.environment, "command", side_effect=self.docker)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.command = patch.object(resources, "command", return_value=["docker"])
        self.command.start()
        self.addCleanup(self.command.stop)

    def runtime(self):
        deploy.stage_sources(ROOT, self.root, "v0.5.0", "fixture")
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

    def test_fresh_runtime_layout_excludes_development_files(self):
        self.runtime()
        self.assertEqual({p.name for p in self.root.iterdir()}, {"stolas", "compose.yaml", ".env", "config", ".stolas"})
        for name in ("tests", ".github", ".gitignore", ".gitattributes", "docs", "VALIDATION.md"):
            self.assertFalse(any(p.name == name for p in (self.root / ".stolas").rglob("*")), name)
        self.assertTrue((self.root / ".stolas/build/agent/runner.py").is_file())
        self.assertTrue((self.root / ".stolas/build/config/apk-aarch64.lock").is_file())
        self.assertTrue((self.root / ".stolas/installer/LICENSE").is_file())

    def test_default_names_and_repeated_configuration_are_stable(self):
        self.runtime()
        self.activate()
        before = resources.read_env(self.root)
        resources.configure(self.root, ["docker"])
        self.assertEqual(resources.read_env(self.root), before)
        self.assertEqual(before["STOLAS_PROJECT_NAME"], "stolas")
        self.assertEqual(before["STOLAS_CONTAINER_NAME"], "stolas")
        self.assertEqual(self.docker.items[0]["image"], "stolas:0.5.0")

    def test_name_collision_requests_a_human_name_and_keeps_foreign_container(self):
        self.runtime()
        foreign = self.docker.container(self.root.parent / "foreign", "stolas", "other-instance")
        with patch("builtins.input", side_effect=["stolas-home"]) as prompt:
            self.activate()
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(resources.read_env(self.root)["STOLAS_PROJECT_NAME"], "stolas-home")
        self.assertIn(foreign, self.docker.items)

    def test_container_mount_order_does_not_change_removal_plan(self):
        self.runtime(); self.activate()
        item = self.docker.items[0]
        item["mounts"].append({"Type": "bind", "Source": str(self.root / "config/local.json"), "Destination": "/config/config.json"})
        before = manage.removal_plan(self.root, ["docker"])
        item["mounts"].reverse()
        self.assertEqual(manage.removal_plan(self.root, ["docker"]), before)

    def test_native_reconfiguration_failure_restores_settings_and_service(self):
        self.runtime(); self.activate()
        original = (self.root / ".env").read_bytes()
        def fail(*args, **kwargs):
            install.write_private(self.root / ".env", (self.root / ".env").read_text().replace("8080", "8081"))
            self.activate()
            return 1
        def restart(root, docker):
            self.assertEqual((root / ".env").read_bytes(), original)
            env = resources.read_env(root)
            self.docker.container(root, env["STOLAS_PROJECT_NAME"], env["STOLAS_INSTANCE_ID"], env["STOLAS_DATA_VOLUME"], id="restored-id")
        # Real Docker creates a new container ID after each handover.
        self.docker.items[0]["id"] = "original-id"
        with patch.object(deploy, "run_installer", side_effect=fail), patch.object(resources, "restart", side_effect=restart) as recovered:
            self.assertEqual(deploy.deploy(self.root, self.root, action="reconfigure"), 1)
        recovered.assert_called_once()
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertEqual([c["id"] for c in self.docker.items], ["restored-id"])

    def test_partial_installation_can_be_uninstalled_without_a_container(self):
        self.runtime()
        resources.configure(self.root, ["docker"])
        name = resources.read_env(self.root)["STOLAS_DATA_VOLUME"]
        with patch("builtins.input", side_effect=["1", "1"]):
            self.assertEqual(manage.uninstall(self.root), 0)
        self.assertIn(name, self.docker.volumes)
        self.assertFalse(self.docker.items)
        self.assertTrue(layout.runtime(self.root))
        self.assertTrue((self.root / "stolas").is_file())
        self.assertFalse((self.root / "tests").exists())
        with patch("builtins.input", side_effect=["1", "1"]):
            self.assertEqual(manage.uninstall(self.root), 0)

    def test_legacy_uninstall_can_restore_a_clean_runtime(self):
        frozen_install(self.root); self.docker.legacy(self.root)
        with patch("builtins.input", side_effect=["1", "1"]):
            manage.uninstall(self.root)
        with patch.object(deploy, "run_installer", side_effect=lambda *a, **k: self.activate()):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 0)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"stolas", "compose.yaml", ".env", "config", ".stolas"})

    def test_volume_collision_also_requests_another_name(self):
        self.runtime()
        self.docker.volumes["stolas_stolas-data"] = {"name": "stolas_stolas-data", "labels": {}, "created": "foreign"}
        with patch("builtins.input", side_effect=["stolas-office"]):
            self.activate()
        self.assertEqual(self.docker.volumes["stolas_stolas-data"]["created"], "foreign")

    def test_two_instances_share_no_managed_resources(self):
        self.runtime(); self.activate()
        first = resources.read_env(self.root)
        second = self.root.parent / "second"
        second.mkdir()
        old_root = self.root
        self.root = second
        self.runtime()
        with patch("builtins.input", side_effect=["stolas-office"]):
            self.activate()
        other = resources.read_env(second)
        for key in ("STOLAS_PROJECT_NAME", "STOLAS_INSTANCE_ID", "STOLAS_DATA_VOLUME"):
            self.assertNotEqual(first[key], other[key])
        self.assertEqual(resources.read_env(old_root), first)

    def test_frozen_v040_migrates_token_config_state_and_exact_volume(self):
        frozen_install(self.root)
        old = self.docker.legacy(self.root)
        token = (self.root / ".env").read_text().splitlines()[0]
        config = (self.root / "config/local.json").read_bytes()
        (self.root / "notes.txt").write_text("foreign")
        with patch.object(deploy, "run_installer", side_effect=lambda *a, **k: self.activate()):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 0)
        self.assertEqual(resources.read_env(self.root)["STOLAS_DATA_VOLUME"], old["mounts"][0]["Name"])
        self.assertIn(token, (self.root / ".env").read_text())
        self.assertEqual((self.root / "config/local.json").read_bytes(), config)
        self.assertEqual((self.root / ".stolas/state/progress.json").read_text(), '{"stage": "complete", "first_status": "route_blocked"}')
        self.assertEqual(len(self.docker.items), 1)
        self.assertEqual(self.docker.items[0]["name"], "/stolas")
        self.assertFalse((self.root / "tests").exists())
        self.assertFalse((self.root / ".github").exists())
        self.assertEqual((self.root / "notes.txt").read_text(), "foreign")

    def test_failed_handover_restores_legacy_files_and_service(self):
        frozen_install(self.root)
        old_env = (self.root / ".env").read_bytes()
        self.docker.legacy(self.root)
        def fail(*a, **kw):
            self.activate()
            return 1
        def restart(root, docker):
            self.assertEqual((root / ".env").read_bytes(), old_env)
            self.docker.legacy(root)
        with patch.object(deploy, "run_installer", side_effect=fail), patch.object(resources, "restart", side_effect=restart) as recovered:
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 1)
        recovered.assert_called_once()
        self.assertEqual((self.root / ".env").read_bytes(), old_env)
        self.assertTrue((self.root / "tools/install.py").is_file())
        self.assertEqual(len(self.docker.items), 1)
        self.assertEqual(self.docker.items[0]["image"], "stolas-133eaddd33:0.4.0")

    def test_interrupt_then_retry_recovers_before_migrating(self):
        frozen_install(self.root); self.docker.legacy(self.root)
        with patch.object(deploy, "run_installer", side_effect=lambda *a, **k: (self.activate(), 130)[1]):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 130)
        with patch.object(resources, "restart", side_effect=lambda root, docker: self.docker.legacy(root)) as restart, patch.object(deploy, "run_installer", side_effect=lambda *a, **k: self.activate()):
            self.assertEqual(deploy.deploy(ROOT, self.root, action="update"), 0)
        restart.assert_called_once()
        self.assertEqual(len(self.docker.items), 1)

    def test_migration_copy_failure_does_not_change_legacy(self):
        frozen_install(self.root)
        before = (self.root / ".env").read_bytes()
        original = deploy.replace_file
        count = 0
        def fail(source, destination):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("injected write failure")
            return original(source, destination)
        with patch.object(deploy, "replace_file", side_effect=fail), self.assertRaises(OSError):
            deploy.stage_sources(ROOT, self.root, "v0.5.0", "")
        self.assertEqual((self.root / ".env").read_bytes(), before)
        self.assertTrue((self.root / "agent/runner.py").is_file())

    def test_uninstall_preserves_history_config_and_offline_command(self):
        self.runtime(); self.activate()
        foreign = self.docker.container(self.root.parent / "n8n", "n8n", "foreign")
        config = (self.root / "config/local.json").read_bytes()
        volume = resources.read_env(self.root)["STOLAS_DATA_VOLUME"]
        for attempt in range(2):
            with patch("builtins.input", side_effect=["1", "1"]):
                self.assertEqual(manage.uninstall(self.root), 0)
        self.assertIn(volume, self.docker.volumes)
        self.assertEqual(self.docker.items, [foreign])
        self.assertEqual((self.root / "config/local.json").read_bytes(), config)
        self.assertTrue((self.root / "stolas").is_file())
        self.assertFalse((self.root / ".stolas/build/agent").exists())

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

    def test_purge_cancel_does_not_mutate_resources(self):
        self.runtime(); self.activate()
        before = copy.deepcopy((self.docker.items, self.docker.volumes, self.docker.images))
        with patch("builtins.input", side_effect=["нет"]):
            self.assertEqual(manage.uninstall(self.root, purge=True), 0)
        self.assertEqual((self.docker.items, self.docker.volumes, self.docker.images), before)

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
        foreign["image_id"] = self.docker.images["stolas:0.5.0"]["id"]
        with patch("builtins.input", side_effect=["1", "1"]):
            manage.uninstall(self.root)
        self.assertIn("stolas:0.5.0", self.docker.images)
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

    def test_legacy_uninstall_uses_actual_mount(self):
        frozen_install(self.root); old = self.docker.legacy(self.root)
        name = old["mounts"][0]["Name"]
        with patch("builtins.input", side_effect=["1", "1"]):
            self.assertEqual(manage.uninstall(self.root), 0)
        self.assertIn(name, self.docker.volumes)
        self.assertFalse(self.docker.items)
        self.assertTrue(layout.runtime(self.root))
        self.assertFalse((self.root / "tests").exists())
        result = subprocess.run([__import__("sys").executable, str(self.root / ".stolas/installer/tools/manage.py"), "--root", str(self.root), "help"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uninstall", result.stdout)
        with patch("builtins.input", side_effect=["1", "1"]):
            self.assertEqual(manage.uninstall(self.root), 0)


if __name__ == "__main__":
    unittest.main()
