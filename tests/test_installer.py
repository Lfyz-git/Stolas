"""Installer orchestration tests; never install packages or contact public services."""
import contextlib
import copy
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
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

    def install(self, configure_only=False):
        self.calls = []

        def command(args, root, capture=False, check=True, **kwargs):
            self.calls.append(args)
            self.assertNotIn("exec", args, "Installation must not run a speed test")
            return subprocess.CompletedProcess(args, 0, "", "")

        facts = {"hostname": "test", "installation": {"directory": str(self.root), "config": False}, "docker": {"available": True, "version": "test"}, "n8n": [], "warnings": []}
        with patch.object(installer.environment, "port_state", return_value="free"), patch.object(installer, "collect_plan", return_value={"config": self.cfg, "api": self.api, "completed": []}), patch.object(installer.environment, "discover", return_value=facts), patch.object(installer, "docker_command", return_value=["docker", "compose"]), patch.object(installer, "request_json", return_value={"status": "ready"}) as health, patch.object(installer, "run", side_effect=command):
            code = installer.install(self.root, configure_only=configure_only)
            if configure_only:
                health.assert_not_called()
            else:
                health.assert_called_once_with("http://127.0.0.1:8080", "/healthz", self.api["STOLAS_API_TOKEN"])
            return code

    def test_reconfigure_health_auth_failure_still_fails(self):
        # The helper patches request_json, so drive install directly here.
        facts = {"hostname": "test", "installation": {"directory": str(self.root), "config": True}, "docker": {"available": True}, "addresses": [], "stolas": [], "n8n": [], "warnings": []}
        plan = {"config": self.cfg, "api": self.api, "completed": []}
        for recover, reason in ((False, "HTTP 401"), (True, "service unavailable")):
            with self.subTest(recover=recover, reason=reason), patch.object(installer, "collect_plan", return_value=plan), patch.object(installer.environment, "discover", return_value=facts), patch.object(installer.environment, "port_state", return_value="free"), patch.object(installer, "docker_command", return_value=["docker", "compose"]), patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(installer, "request_json", side_effect=RuntimeError(reason)), patch.object(installer, "first_test") as heavy:
                with self.assertRaisesRegex(RuntimeError, reason):
                    installer.install(self.root, recover=recover)
                heavy.assert_not_called()

    def test_install_starts_service_without_measurement(self):
        self.assertEqual(self.install(), 0)
        self.assertEqual(sum("exec" in args for args in self.calls), 0)
        self.assertTrue(any("--wait" in args for args in self.calls))
        self.assertTrue(any("validate" in args for args in self.calls))
        self.assertFalse((self.root / "n8n/local.json").exists())
        self.assertNotIn(self.api["STOLAS_API_TOKEN"], self.output.getvalue())

    def test_invalid_config_does_not_overwrite_existing_files(self):
        self.install(configure_only=True)
        before = (self.root / "config/local.json").read_bytes()
        self.cfg["parallel"] = 0
        with self.assertRaises(ValueError):
            self.install(configure_only=True)
        self.assertEqual((self.root / "config/local.json").read_bytes(), before)

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

    def test_public_plain_http_and_credentials_in_urls_are_rejected(self):
        for value in ("http://example.com", "https://user:pass@example.com", "https://example.com?key=secret", "https://example.com:99999"):
            with self.assertRaises(ValueError):
                installer.url(value)
        self.assertEqual(installer.url("http://127.0.0.1:5678/"), "http://127.0.0.1:5678")

if __name__ == "__main__":
    unittest.main()
