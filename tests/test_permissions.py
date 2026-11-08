"""Managed ownership and safe errors; elevated Linux test runs only in CI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/diagnostics.py").exists()
if HAS_TOOLS:
    from tools import deploy, install


@unittest.skipUnless(HAS_TOOLS, "Management is outside production image")
class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime"
        self.root.mkdir()
        deploy.stage_sources(ROOT, self.root, "test", "")

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
owned=root/'root-owned-installation'
owned.mkdir()
private=owned/'.stolas/state/status.json'
deploy.atomic_json(private,{'stage':'root-owned'})
assert private.stat().st_uid == 0
assert private.parent.stat().st_uid == 0
private.unlink(); private.parent.rmdir(); (owned/'.stolas').rmdir(); owned.rmdir()
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
