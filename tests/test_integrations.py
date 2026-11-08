import contextlib
import copy
import io
import json
import http.cookiejar
import os
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest.mock import patch
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/integrate.py").exists()
if HAS_TOOLS:
    from tools import integrate, install


@unittest.skipUnless(HAS_TOOLS, "Integration wizard is not shipped in Core image")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "n8n").mkdir()
        self.options = dict(mode="api", endpoint="http://127.0.0.1:8080", url="https://n8n.example.test", key="private-api-key", chat_id="123", hours=3, timezone="Etc/UTC")
        self.api = dict(STOLAS_API_TOKEN="a" * 40, STOLAS_LISTEN="127.0.0.1", STOLAS_PORT="8080")
        self.data = integrate.workflow(ROOT, self.options)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)


    def test_ambiguous_previous_post_never_repeats(self):
        path = self.root / "n8n/install-state.json"
        path.write_text(json.dumps(dict(url=self.options["url"], pending="workflow", credentials=[])))
        with patch.object(integrate, "request_json") as request:
            with self.assertRaisesRegex(ValueError, "мог создать"):
                integrate.connect_n8n(self.root, self.options, self.api, self.data)
        request.assert_not_called()

    def installed_core(self):
        (self.root / ".env").write_text("".join(k + "=" + v + "\n" for k, v in self.api.items()))
        (self.root / "n8n/stolas.json").write_bytes((ROOT / "n8n/stolas.json").read_bytes())
        return {"docker": {"available": True}, "n8n": [], "addresses": [], "stolas": [], "timezone": "Etc/UTC"}

    def test_separate_export_wizard_never_asks_api_key_or_restarts_core(self):
        facts = self.installed_core()
        facts["timezone"] = "Europe/Moscow"
        (self.root / "n8n/settings.json").write_text(json.dumps({"timezone": "Europe/Berlin"}))
        original = (self.root / ".env").read_bytes()
        with patch.dict(os.environ, {"TZ": "Etc/UTC"}), patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "request_json", return_value={"status": "ready"}), patch.object(integrate, "run") as run, patch("builtins.input", side_effect=["native", "", "trusted", "123", "", "", "apply"]) as prompt, patch("getpass.getpass") as secret:
            self.assertEqual(integrate.integrate(self.root, "export"), 0)
        secret.assert_not_called(); run.assert_not_called()
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertFalse(any("Часовой пояс" in c.args[0] for c in prompt.call_args_list))
        exported = json.loads((self.root / "n8n/local.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["settings"]["timezone"], "Europe/Moscow")
        for node in exported["nodes"]:
            if node["name"] in ("Settings", "Summary settings"):
                self.assertIn('"timezone": "Europe/Moscow"', node["parameters"]["jsCode"])

    def test_failed_network_change_restores_core_and_keeps_manual_export(self):
        facts = self.installed_core()
        original = (self.root / ".env").read_bytes()
        def topology(api, *args):
            api["STOLAS_LISTEN"] = "172.18.0.1"
            return dict(topology="docker", docker_id="abc", endpoint="http://172.18.0.1:8080")
        with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate, "collect_topology", side_effect=topology), patch.object(integrate, "validate_discovered_endpoint"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "check_connection", side_effect=RuntimeError("exception-private-secret")), patch.object(integrate, "run") as run, patch("builtins.input", side_effect=["123", "", "", "apply"]):
            self.assertEqual(integrate.integrate(self.root, "export"), 2)
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all("up" in call.args[0] for call in run.call_args_list))
        self.assertTrue((self.root / "n8n/local.json").exists())
        self.assertNotIn("exception-private-secret", self.output.getvalue())

    def test_wildcard_requires_separate_confirmation_and_a_client_endpoint(self):
        facts = self.installed_core()
        facts["networks"] = {}
        def collect(answers):
            with patch("builtins.input", side_effect=answers), patch.object(integrate.environment, "port_state", return_value="free"):
                return integrate.collect_topology(self.api, facts, self.root)
        with self.assertRaises(install.Cancel):
            collect(["remote", "1", "0.0.0.0:8080", "cancel"])
        options = collect(["remote", "1", "0.0.0.0:8080", "allow_bind", "https://stolas.example.test"])
        self.assertEqual(self.api["STOLAS_BIND_CONFIRMED"], "1")
        self.assertEqual(self.api["STOLAS_LISTEN"], "0.0.0.0")
        self.assertEqual(options["endpoint"], "https://stolas.example.test")
        cfg = copy.deepcopy(install.DEFAULT)
        cfg["route"]["mode"] = "off"
        install.validate_plan(dict(api=self.api, config=cfg))
        self.api.pop("STOLAS_BIND_CONFIRMED")
        with self.assertRaises(ValueError):
            install.validate_plan(dict(api=self.api, config=cfg))



@unittest.skipUnless(HAS_TOOLS and os.getenv("STOLAS_N8N_INTEGRATION") == "1", "Opt-in isolated pinned n8n API integration")
class RealN8nTests(unittest.TestCase):
    def test_existing_credentials_and_inactive_workflow_with_real_n8n(self):
        name = "stolas-n8n-test-" + uuid.uuid4().hex[:12]
        image = "docker.n8n.io/n8nio/n8n:2.42.6@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a"
        def docker(*args):
            return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=120).stdout.strip()
        docker("run", "-d", "--name", name, "-p", "127.0.0.1::5678",
               "-e", "TZ=Etc/UTC", "-e", "GENERIC_TIMEZONE=Etc/UTC",
               "-e", "N8N_ENCRYPTION_KEY=isolated-fixture-only-never-deployed",
               "-e", "N8N_DIAGNOSTICS_ENABLED=false", "-e", "N8N_VERSION_NOTIFICATIONS_ENABLED=false",
               "-e", "N8N_PERSONALIZATION_ENABLED=false", "-e", "N8N_SECURE_COOKIE=false", image)
        self.addCleanup(lambda: docker("rm", "-fv", name))
        port = docker("port", name, "5678/tcp").rsplit(":", 1)[1]
        base = "http://127.0.0.1:" + port
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def rest(path, body=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "browser-id": "stolas-ci"})
            with opener.open(req, timeout=10) as response:
                value = json.load(response)
                return value.get("data", value)
        deadline = time.monotonic() + 100
        while True:
            try:
                # Liveness is available before migrations and routes are ready.
                with opener.open(base + "/healthz/readiness", timeout=2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail("isolated n8n failed to start")
                time.sleep(.5)
        # This is fixture setup in a disposable instance, never the user's n8n.
        rest("/rest/owner/setup", {"email": "ci@example.test", "firstName": "Stolas", "lastName": "CI", "password": "FixtureOnlyN8n123!"})
        key = rest("/rest/api-keys", {"label": "Stolas integration test", "scopes": ["credential:list", "credential:create", "workflow:create", "workflow:read"], "expiresAt": None})["rawApiKey"]
        # Different from the disposable n8n container's UTC default: the host
        # zone already verified by the wizard must survive the actual API import.
        options = dict(mode="api", endpoint="http://127.0.0.1:8080", url=base, key=key, chat_id="123", hours=3, timezone="Europe/Moscow")
        for kind, values in (("telegramApi", {"accessToken": "123:fixture-not-a-real-bot", "baseUrl": "https://api.telegram.org"}), ("httpHeaderAuth", {"name": "Authorization", "value": "Bearer " + "a" * 40})):
            integrate.request_json(base + "/api/v1", "/credentials", key, {"name": "Existing " + kind, "type": kind, "data": values}, n8n=True)
        before = integrate.list_credentials(options)
        with tempfile.TemporaryDirectory() as directory, patch("getpass.getpass") as secret:
            root = Path(directory)
            (root / "n8n").mkdir()
            (root / "n8n/stolas.json").write_bytes((ROOT / "n8n/stolas.json").read_bytes())
            (root / ".env").write_text("STOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\nSTOLAS_API_TOKEN=" + "a" * 40 + "\n")
            facts = dict(docker={"available": True}, stolas=[], n8n=[], addresses=[], timezone="Europe/Moscow")
            # Core health is covered separately; all n8n API calls here are real.
            answers = ["native", "", "trusted", "123", "", "", base, "trusted", key, "apply", "1", "connect"]
            with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "check_connection"), patch("builtins.input", side_effect=answers):
                self.assertEqual(integrate.integrate(root, "api"), 0)
            saved = json.loads((root / "n8n/settings.json").read_text())
            self.assertEqual(saved["url"], base)
            self.assertTrue(saved["http_trusted"] and saved["n8n_http_trusted"])
            self.assertNotIn(key, (root / "n8n/settings.json").read_text())
            state = json.loads((root / "n8n/install-state.json").read_text())
            workflow = integrate.request_json(base + "/api/v1", "/workflows/" + state["workflow_id"], key, n8n=True)
        secret.assert_not_called()
        self.assertFalse(workflow["active"])
        self.assertEqual(workflow["settings"]["timezone"], "Europe/Moscow")
        for settings_node in (n for n in workflow["nodes"] if n["name"] in ("Settings", "Summary settings")):
            self.assertIn('"timezone": "Europe/Moscow"', settings_node["parameters"]["jsCode"])
        after = integrate.list_credentials(options)
        self.assertEqual({c["id"] for c in before}, {c["id"] for c in after})
        tg = next(c for c in before if c["type"] == "telegramApi")
        node = next(n for n in workflow["nodes"] if n["name"] == "Telegram alert")
        self.assertEqual(node["credentials"]["telegramApi"]["id"], tg["id"])
