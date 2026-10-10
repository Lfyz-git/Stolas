"""PATH ownership and actual Linux shell boundaries; no host configuration changes."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/entrypoints.py").is_file()
if HAS_TOOLS:
    from tools import deploy, entrypoints, install, layout, manage, resources
    from test_runtime import DockerFixture, frozen_install


@unittest.skipUnless(HAS_TOOLS, "Manager is outside the production image")
class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stolas-path-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "installation with spaces ' quoted"
        self.root.mkdir()
        self.binary = self.home / "bin with spaces"
        deploy.stage_sources(ROOT, self.root, "v0.5.1", "fixture")
        (self.root / ".env").write_text("STOLAS_API_TOKEN=" + "a" * 40 + "\nSTOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\n")
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/local.json").write_text(json.dumps(install.DEFAULT))
        redirect = contextlib.redirect_stdout(io.StringIO())
        self.output = redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def install(self, root=None, name="stolas"):
        with patch.object(entrypoints.shutil, "which", return_value=None):
            return entrypoints.install(root or self.root, self.binary, name)

    def test_owned_wrapper_idempotent_no_secrets_and_safe_repeated_removal(self):
        record = self.install()
        path = Path(record["path"])
        self.assertEqual(self.install(), record)
        self.assertTrue(entrypoints.owned(self.root, record))
        self.assertNotIn("a" * 40, path.read_text())
        self.assertNotIn(".env", path.read_text())
        entrypoints.remove(self.root)
        entrypoints.remove(self.root)
        self.assertFalse(path.exists())

    def test_foreign_files_path_commands_and_other_instances_never_overwritten(self):
        self.binary.mkdir()
        path = self.binary / "stolas"
        path.write_text("foreign-command")
        with self.assertRaises(FileExistsError):
            self.install()
        self.assertEqual(path.read_text(), "foreign-command")
        with patch.object(entrypoints.shutil, "which", return_value="/foreign/stolas-home"), self.assertRaises(FileExistsError):
            entrypoints.install(self.root, self.binary, "stolas-home")
        self.assertFalse((self.binary / "stolas-home").exists())
        record = self.install(name="stolas-first")
        other = self.home / "second"
        other.mkdir()
        deploy.stage_sources(ROOT, other, "v0.5.1", "fixture")
        with self.assertRaises(FileExistsError):
            self.install(other, "stolas-first")
        second = self.install(other, "stolas-second")
        entrypoints.remove(other)
        self.assertTrue(entrypoints.owned(self.root, record))
        self.assertFalse(Path(second["path"]).exists())

    def test_changed_wrapper_is_preserved_and_partial_install_is_rejected(self):
        record = self.install()
        path = Path(record["path"])
        path.write_text("user-changed")
        entrypoints.remove(self.root)
        self.assertEqual(path.read_text(), "user-changed")
        self.assertIn("изменена и сохранена", self.output.getvalue())
        (self.root / ".stolas/installer/tools/install.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "не завершена"):
            self.install(name="partial")
        self.assertFalse((self.binary / "partial").exists())

    def test_failed_state_write_removes_only_wrapper_created_by_this_attempt(self):
        with patch.object(deploy, "atomic_json", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.install()
        self.assertFalse((self.binary / "stolas").exists())
        self.assertFalse(entrypoints.read(self.root))

    def test_system_command_removal_requires_rights_before_any_docker_mutation(self):
        record = self.install()
        docker = DockerFixture()
        real_access = os.access
        def access(path, mode):
            return False if Path(path) == Path(record["path"]).parent else real_access(path, mode)
        with patch.object(resources.environment, "command", side_effect=docker), patch.object(resources, "command", return_value=["docker"]), patch.object(entrypoints.os, "access", side_effect=access), patch("builtins.input", side_effect=["remove", "confirm"]), self.assertRaisesRegex(RuntimeError, "Нет прав на удаление команды"):
            manage.uninstall(self.root)
        self.assertTrue(entrypoints.owned(self.root, record))
        self.assertFalse(any(c[0] in ("stop", "rm") or c[:2] in (["image", "rm"], ["volume", "rm"]) for c in docker.calls))
        self.assertFalse(resources.load(self.root, "resources.json").get("uninstalled"))

    @unittest.skipUnless(hasattr(os, "chown"), "POSIX metadata owner after sudo")
    def test_sudo_command_setup_retains_metadata_owner_of_installation(self):
        parent = (self.root / ".stolas/state").stat()
        with patch.object(entrypoints.os, "geteuid", return_value=0), patch.object(entrypoints.os, "chown") as chown:
            self.install()
        self.assertTrue(any(c.args[1:] == (parent.st_uid, parent.st_gid) for c in chown.call_args_list))

    def test_user_without_sudo_manual_path_decline_and_explicit_system_confirmation(self):
        userbin = self.home / ".local/bin"
        with patch.object(Path, "home", return_value=self.home), patch.dict(os.environ, {"PATH": str(userbin)}), patch("builtins.input", side_effect=[""]):
            entrypoints.configure(self.root)
        record = entrypoints.read(self.root)
        self.assertEqual(Path(record["path"]), userbin / "stolas")
        entrypoints.remove(self.root)
        with patch.object(Path, "home", return_value=self.home), patch.dict(os.environ, {"PATH": "/nonexistent"}), patch("builtins.input", side_effect=[""]):
            entrypoints.configure(self.root)
        self.assertTrue(entrypoints.read(self.root)["declined"])
        with patch.object(Path, "home", return_value=self.home), patch.dict(os.environ, {"PATH": "/nonexistent"}), patch.object(entrypoints.shutil, "which", return_value=None):
            entrypoints.configure(self.root, "user")
        self.assertIn("export PATH=", self.output.getvalue())
        self.assertFalse((self.home / ".profile").exists())
        entrypoints.remove(self.root)
        with patch("builtins.input", side_effect=[""]), patch.object(entrypoints, "install") as create:
            entrypoints.configure(self.root, "system")
        create.assert_not_called()
        with patch("builtins.input", side_effect=["apply"]), patch.object(entrypoints.os, "access", return_value=False), self.assertRaises(PermissionError):
            entrypoints.configure(self.root, "system")

    def test_update_and_rollback_keep_owned_command_even_to_version_before_path_feature(self):
        import tarfile
        old = self.home / "old source"
        with tarfile.open(ROOT / "tests/fixtures/v0.5.0.tar.gz") as archive:
            for member in archive:
                if member.isfile():
                    path = old / member.name
                    self.assertNotIn("..", Path(member.name).parts)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.extractfile(member).read())
        source = next(old.iterdir())
        # An installed v0.5.0, not a source checkout masquerading as runtime.
        deploy.finalize(self.root, deploy.stage_sources(source, self.root, "v0.5.0", "frozen"))
        deploy.finalize(self.root, deploy.stage_sources(ROOT, self.root, "v0.5.1", "fixture"))
        record = self.install()
        backup = deploy.read_json(self.root / ".stolas/state/transaction.json")["backup"]
        deploy.rollback_files(self.root, self.root / backup, configure_only=True, preserve_manager=True)
        self.assertEqual(resources.core_version(self.root), "0.5.0")
        self.assertEqual(deploy.read_json(self.root / ".stolas/state/managed.json")["ref"], "v0.5.0")
        self.assertTrue((self.root / ".stolas/installer/tools/entrypoints.py").is_file())
        self.assertIn("entrypoints.remove", (self.root / ".stolas/installer/tools/manage.py").read_text())
        self.assertTrue(entrypoints.owned(self.root, record))
        entrypoints.remove(self.root)
        self.assertFalse(Path(record["path"]).exists())

    @unittest.skipIf(os.name == "nt", "Purge unlinks a held POSIX lock; supported platform is Linux")
    def test_normal_and_purge_uninstall_remove_owned_wrapper_and_preserve_foreign(self):
        docker = DockerFixture()
        with patch.object(resources.environment, "command", side_effect=docker), patch.object(resources, "command", return_value=["docker"]):
            resources.configure(self.root, ["docker"])
            record = self.install()
            foreign = self.binary / "neighbour"
            foreign.write_text("foreign")
            with patch("builtins.input", side_effect=["remove", "confirm"]):
                self.assertEqual(manage.uninstall(self.root), 0)
            self.assertFalse(Path(record["path"]).exists())
            self.assertTrue((self.root / "stolas").is_file())
            self.assertTrue(docker.volumes)
            record = self.install()
            with patch("builtins.input", side_effect=["УДАЛИТЬ"]):
                self.assertEqual(manage.uninstall(self.root, purge=True), 0)
            self.assertFalse(Path(record["path"]).exists())
            self.assertEqual(foreign.read_text(), "foreign")
            self.assertFalse(docker.volumes)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux executable PATH wrapper")
    def test_path_wrapper_preserves_cwd_arguments_exit_status_signals_and_missing_target(self):
        probe = self.home / "probe.py"
        probe.write_text("import json,os,signal,sys\nprint(json.dumps({'cwd':os.getcwd(),'args':sys.argv[1:]}),flush=True)\nif sys.argv[1:] == ['signal']: os.kill(os.getpid(),signal.SIGTERM)\nsys.exit(7)\n")
        (self.root / "stolas").write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(probe)]) + ' "$@"\n')
        (self.root / "stolas").chmod(0o755)
        record = self.install()
        env = {**os.environ, "PATH": str(self.binary) + os.pathsep + os.environ["PATH"]}
        cwd = Path.cwd()
        args = ["history", "argument with spaces", "';$(touch evil);$HOME"]
        result = subprocess.run(["stolas", *args], cwd=self.home, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"cwd": str(self.home), "args": args})
        self.assertEqual(Path.cwd(), cwd)
        self.assertFalse((self.home / "evil").exists())
        result = subprocess.run(["stolas", "signal"], cwd=self.home, env=env, capture_output=True)
        self.assertEqual(result.returncode, -signal.SIGTERM)
        (self.root / "stolas").unlink()
        result = subprocess.run(["stolas", "help"], cwd=self.home, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 127)
        self.assertIn("Установка Stolas не найдена", result.stderr)
        entrypoints.remove(self.root)
        self.assertFalse(Path(record["path"]).exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux CLI from another cwd")
    def test_native_cli_routes_all_subcommands_to_installation_from_path(self):
        self.install()
        stub = self.binary / "docker"
        log = self.home / "docker.log"
        stub.write_text("#!" + sys.executable + "\n" + '''import json,os,pathlib,sys
args=sys.argv[1:]
with pathlib.Path(os.environ['TEST_DOCKER_LOG']).open('a') as f: f.write(json.dumps({'args':args,'cwd':os.getcwd()})+'\\n')
if 'context' in args: print('unix:///var/run/docker.sock')
elif 'info' in args: print('linux x86_64 fixture')
elif 'logs' in args: sys.exit(7)
elif 'exec' in args: print(json.dumps({'status':'ok','history':[]}))
elif 'config' in args: print('{"volumes":{}}')
elif args[:2] == ['volume','inspect']: sys.exit(1)
''')
        stub.chmod(0o755)
        env = {**os.environ, "PATH": str(self.binary) + os.pathsep + os.environ["PATH"], "TEST_DOCKER_LOG": str(log)}
        commands = [(["help"], "", 0), (["diagnose", "--json"], "", 0), (["history"], "", 0),
                    (["run", "--json"], "", 0), (["run"], "", 0), (["logs"], "", 7),
                    (["configure"], ":cancel\n", 3), (["update", "--version", "invalid"], "", 1),
                    (["rollback"], "", 0), (["integrate", "n8n", "--mode", "export"], ":cancel\n", 3),
                    (["uninstall"], "cancel\n", 0)]
        for args, answer, expected in commands:
            with self.subTest(command=args):
                result = subprocess.run(["stolas", *args], cwd=self.home, env=env, input=answer, text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("a" * 40, result.stdout + result.stderr)
        for call in map(json.loads, log.read_text().splitlines()):
            if "--project-directory" in call["args"]:
                self.assertEqual(call["args"][call["args"].index("--project-directory") + 1], str(self.root))
                if "exec" in call["args"] or "logs" in call["args"]:
                    self.assertEqual(call["cwd"], str(self.root))


if __name__ == "__main__":
    unittest.main()
