import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/install.py").is_file()
if HAS_TOOLS:
    from tools import install, environment, deploy, manage


@unittest.skipUnless(HAS_TOOLS, "Host installer outside Core image")
class PortUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        self.config = copy.deepcopy(install.DEFAULT)
        self.config["route"]["mode"] = "off"
        (self.root / "config/local.json").write_text(json.dumps(self.config))
        (self.root / ".env").write_text("STOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\nSTOLAS_API_TOKEN=" + "a" * 40)
        self.api = install.read_api(self.root)
        self.facts = {"hostname": "test", "stolas": [{"id": "owned", "mode": "host"}], "docker": {"available": True, "command": ["sudo", "-n", "docker"]}, "tools": {"docker": True}, "installation": {"directory": str(self.root), "config": True}, "warnings": []}
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    def test_update_keeps_saved_endpoint_without_port_or_health_probe(self):
        with patch.object(environment, "port_state", return_value="busy") as port, patch.object(install, "request_json", side_effect=RuntimeError("health failure")) as health, patch("builtins.input", side_effect=["apply"]):
            plan = install.collect_plan(self.root, self.config, self.facts, reuse=True)
        port.assert_not_called()
        health.assert_not_called()
        self.assertEqual(plan["api"], self.api)

    def test_update_entrypoints_stop_before_download_lock_or_staging(self):
        deploy.stage_sources(ROOT, self.root, "fixture", "")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.facts["docker"] = {"available": False, "reason": "Нет прав к Docker socket"}
        with patch.object(environment, "discover", return_value=self.facts), patch.object(deploy, "stage_sources") as stage, patch.object(deploy, "install_lock") as lock, self.assertRaisesRegex(RuntimeError, "до настройки"):
            deploy.deploy(ROOT, self.root, action="update")
        stage.assert_not_called(); lock.assert_not_called()
        with patch.object(environment, "discover", return_value=self.facts), patch.object(manage.urllib.request, "urlopen") as fetch, self.assertRaisesRegex(RuntimeError, "до настройки"):
            manage.update(self.root)
        fetch.assert_not_called()
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_foreign_or_unknown_busy_endpoint_is_rejected_before_handover(self):
        self.facts["port_bindings"] = [{"address": "0.0.0.0", "port": 8080, "owned": False}]
        with patch.object(environment, "port_state", return_value="busy"), self.assertRaisesRegex(RuntimeError, "подтверждён Docker"):
            install.check_api_port(self.api, self.facts, allow_existing=True)
        self.facts["port_bindings"] = []
        self.facts["stolas"] = []
        with patch.object(environment, "port_state", return_value="busy"), self.assertRaisesRegex(RuntimeError, "не подтверждена"):
            install.check_api_port(self.api, self.facts, allow_existing=True)
        self.facts["stolas"] = [{"id": "owned"}]
        with patch.object(environment, "port_state", return_value="busy"), self.assertRaisesRegex(RuntimeError, "не подтверждена"):
            install.check_api_port(self.api, self.facts)  # Explicit new endpoint.
