"""Managed ownership and safe errors; elevated Linux test runs only in CI."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/diagnostics.py").exists()
if HAS_TOOLS:
    from tools import deploy, diagnostics, install, layout, manage


@unittest.skipUnless(HAS_TOOLS, "Management is outside production image")
class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime"
        self.root.mkdir()
        deploy.stage_sources(ROOT, self.root, "test", "")

    def test_os_errors_show_path_type_and_absolute_or_owned_path_hint_without_payload(self):
        for error in (PermissionError(errno.EACCES, "private-token", str(self.root / ".stolas/state/install.lock")), FileNotFoundError(errno.ENOENT, "private-token", str(self.root / ".env")), OSError(errno.EROFS, "private-token", str(self.root / ".env")), KeyError("private-token")):
            with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(manage, "update", side_effect=error), patch("sys.argv", ["stolas", "--root", str(self.root), "update"]):
                self.assertEqual(manage.main(), 1)
            self.assertNotIn("private-token", output.getvalue())
            self.assertIn(str(self.root / "stolas"), " ".join(output.getvalue().split()))
            if isinstance(error, OSError):
                self.assertIn(str(self.root), output.getvalue())
        record = json.loads((self.root / ".stolas/state/error.json").read_text())
        self.assertEqual(record["exception"], "KeyError")
        self.assertNotIn("private-token", json.dumps(record))

    def test_preflight_permission_failure_precedes_transaction_or_configuration(self):
        path = str(self.root / ".stolas/state/managed.json")
        report = {"paths": [{"path": path, "readable": False, "writable": True}]}
        before = (self.root / ".stolas/state/transaction.json").read_bytes()
        with patch.object(layout, "access_report", return_value=report), self.assertRaises(PermissionError) as caught:
            deploy.stage_sources(ROOT, self.root, "new", "")
        self.assertEqual(caught.exception.filename, path)
        self.assertEqual((self.root / ".stolas/state/transaction.json").read_bytes(), before)
        with patch.object(layout, "access_report", return_value=report), patch.object(install, "collect_plan") as wizard, self.assertRaises(PermissionError):
            install.install(self.root)
        wizard.assert_not_called()

    def test_diagnose_reports_metadata_read_only_without_secret_contents(self):
        install.write_private(self.root / ".env", "private-token")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = layout.access_report(self.root)
        self.assertNotIn("private-token", json.dumps(report))
        self.assertTrue(any(r["path"].endswith("managed.json") and "uid" in r for r in report["paths"]))
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)

    @unittest.skipUnless(sys.platform.startswith("linux") and os.getenv("STOLAS_PRIVILEGED_TEST") == "1", "CI-only isolated sudo filesystem fixture")
    def test_real_sudo_stage_atomic_private_backups_and_user_rollback(self):
        # Entire fixture is in a private temporary directory. No host ownership,
        # accounts, sockets, packages or Docker resources are changed here.
        uid = os.geteuid()
        self.assertNotEqual(uid, 0)
        install.write_private(self.root / ".env", "STOLAS_API_TOKEN=private-token\n")
        (self.root / "config").mkdir(exist_ok=True)
        install.write_private(self.root / "config/local.json", json.dumps(install.DEFAULT))
        script = """import sys
from pathlib import Path
from tools import deploy,install,resources
root=Path(sys.argv[1]); source=Path(sys.argv[2])
(root/'foreign-note').write_text('leave-root-owned')
backup=deploy.stage_sources(source,root,'sudo-update','')
resources.save(root,'identity.json',{'instance':'fixture'})
install.write_private(root/'.env','STOLAS_API_TOKEN=second-token\\n')
deploy.atomic_json(root/'.stolas/state/progress.json',{'stage':'complete'})
print(backup)
"""
        result = subprocess.run(["sudo", "-n", sys.executable, "-c", script, str(self.root), str(ROOT)], cwd=ROOT, text=True, capture_output=True, check=True)
        backup = Path(result.stdout.strip())
        for p in self.root.rglob("*"):
            if p.name != "foreign-note":
                self.assertEqual(p.stat().st_uid, uid, str(p))
        self.assertEqual((self.root / "foreign-note").stat().st_uid, 0)
        self.assertEqual((self.root / ".stolas/state/identity.json").stat().st_mode & 0o777, 0o600)
        deploy.restore(self.root, backup)
        self.assertIn("private-token", (self.root / ".env").read_text())
        deploy.atomic_json(self.root / ".stolas/state/status.json", {"stage": "user-configure"})
        deploy.stage_sources(ROOT, self.root, "user-repeat", "")
        self.assertEqual((self.root / "foreign-note").stat().st_uid, 0)


if __name__ == "__main__":
    unittest.main()
