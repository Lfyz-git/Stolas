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
    from tools import deploy, entrypoints, install


@unittest.skipUnless(HAS_TOOLS, "Manager is outside the production image")
class EntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stolas-path-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "installation with spaces ' quoted"
        self.root.mkdir()
        self.binary = self.home / "bin with spaces"
        deploy.stage_sources(ROOT, self.root, "v0.6.5", "fixture")
        (self.root / ".env").write_text("STOLAS_API_TOKEN=" + "a" * 40 + "\nSTOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\n")
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/local.json").write_text(json.dumps(install.DEFAULT))
        redirect = contextlib.redirect_stdout(io.StringIO())
        self.output = redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def install(self, root=None, name="stolas"):
        with patch.object(entrypoints.shutil, "which", return_value=None):
            return entrypoints.install(root or self.root, self.binary, name)

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
        deploy.stage_sources(ROOT, other, "v0.6.5", "fixture")
        with self.assertRaises(FileExistsError):
            self.install(other, "stolas-first")
        second = self.install(other, "stolas-second")
        entrypoints.remove(other)
        self.assertTrue(entrypoints.owned(self.root, record))
        self.assertFalse(Path(second["path"]).exists())

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

if __name__ == "__main__":
    unittest.main()
