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
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/integrate.py").exists()
if HAS_TOOLS:
    from tools import integrate


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
        self.items = [self.credential("tg", "telegramApi"), self.credential("api", "httpHeaderAuth")]
        self.calls = []
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def credential(self, id, type, project="p1"):
        return dict(id=id, name=id + " credential", type=type, shared=[dict(projectId=project, project=dict(id=project, name=project))], data=dict(accessToken="must-be-discarded"))

    def request(self, base, path, token, body=None, n8n=False):
        self.calls.append((path, copy.deepcopy(body)))
        if body is None:
            return {"data": self.items, "nextCursor": None}
        return {"id": "new-id"}

    def connect(self, answers=("connect",)):
        with patch.object(integrate, "request_json", side_effect=self.request), patch("builtins.input", side_effect=answers), patch("getpass.getpass") as secret:
            result = integrate.connect_n8n(self.root, self.options, self.api, self.data)
        secret.assert_not_called()
        return result

    def test_existing_credentials_are_reused_without_creating_or_reading_secrets(self):
        self.assertTrue(self.connect())
        posts = [(p, b) for p, b in self.calls if b]
        self.assertEqual([p for p, b in posts], ["/workflows"])
        payload = posts[0][1]
        self.assertEqual(payload["projectId"], "p1")
        telegram = next(n for n in payload["nodes"] if n["name"] == "Telegram alert")
        self.assertEqual(telegram["credentials"]["telegramApi"], dict(id="tg", name="tg credential"))
        self.assertNotIn("active", payload)
        for file in self.root.rglob("*.json*"):
            for secret in ("must-be-discarded", self.options["key"], self.api["STOLAS_API_TOKEN"]):
                self.assertNotIn(secret, file.read_text())
        self.calls.clear()
        self.assertTrue(self.connect(answers=()))
        self.assertEqual(self.calls, [])

    def test_multiple_credentials_use_named_selection(self):
        self.items.insert(1, self.credential("tg2", "telegramApi"))
        self.assertTrue(self.connect(("2", "connect")))
        body = next(b for p, b in self.calls if b)
        telegram = next(n for n in body["nodes"] if n["name"] == "Telegram alert")
        self.assertEqual(telegram["credentials"]["telegramApi"]["id"], "tg2")
        self.assertIn("tg2 credential", self.output.getvalue())

    def test_missing_credentials_exports_and_does_not_post(self):
        self.items = []
        self.assertFalse(self.connect(()))
        self.assertFalse(any(b for p, b in self.calls))
        self.assertTrue((self.root / "n8n/local.json").exists())

    def test_permissions_and_older_api_fall_back_to_export(self):
        for error in ("HTTP 401", "HTTP 403", "HTTP 404", "HTTP 405", "unavailable"):
            with patch.object(integrate, "request_json", side_effect=RuntimeError(error)), patch("getpass.getpass") as hidden:
                self.assertFalse(integrate.connect_n8n(self.root, self.options, self.api, self.data))
            hidden.assert_not_called()

    def test_paginated_metadata_strips_secrets_and_rejects_cursor_loops(self):
        pages = [{"data": self.items[:1], "nextCursor": "next/page"}, {"data": self.items[1:], "nextCursor": None}]
        with patch.object(integrate, "request_json", side_effect=pages) as request:
            items = integrate.list_credentials(self.options)
        self.assertEqual(len(items), 2)
        self.assertIn("cursor=next%2Fpage", request.call_args.args[1])
        self.assertNotIn("must-be-discarded", json.dumps(items))
        with patch.object(integrate, "request_json", return_value=pages[0]), self.assertRaises(ValueError):
            integrate.list_credentials(self.options)

    def test_project_mismatch_or_unknown_sharing_does_not_post(self):
        self.items[1] = self.credential("api", "httpHeaderAuth", "p2")
        self.assertFalse(self.connect(()))
        self.assertFalse(any(b for p, b in self.calls))
        for item in self.items:
            item.pop("shared")
        self.assertFalse(self.connect(()))

    def test_only_stolas_header_credential_can_be_created_with_explicit_choice(self):
        self.items = self.items[:1]
        self.assertTrue(self.connect(("create", "connect")))
        posts = [(p, b) for p, b in self.calls if b]
        self.assertEqual([p for p, b in posts], ["/credentials", "/workflows"])
        self.assertEqual(posts[0][1]["type"], "httpHeaderAuth")
        self.assertEqual(posts[0][1]["data"]["value"], "Bearer " + self.api["STOLAS_API_TOKEN"])

    def test_ambiguous_previous_post_never_repeats(self):
        path = self.root / "n8n/install-state.json"
        path.write_text(json.dumps(dict(url=self.options["url"], pending="workflow", credentials=[])))
        with self.assertRaisesRegex(ValueError, "мог создать"):
            self.connect(())
        self.assertFalse(self.calls)

    def test_export_is_inactive_and_never_needs_n8n_api_key(self):
        options = {k: v for k, v in self.options.items() if k != "key"}
        options["mode"] = "export"
        with patch.object(integrate, "request_json") as request:
            self.assertTrue(integrate.connect_n8n(self.root, options, self.api, self.data))
        request.assert_not_called()
        data = json.loads((self.root / "n8n/local.json").read_text())
        self.assertFalse(data["active"])
        self.assertTrue(all("credentials" not in n for n in data["nodes"]))

    def installed_core(self):
        (self.root / ".env").write_text("".join(k + "=" + v + "\n" for k, v in self.api.items()))
        (self.root / "n8n/stolas.json").write_bytes((ROOT / "n8n/stolas.json").read_bytes())
        return {"docker": {"available": True}, "n8n": [], "addresses": [], "stolas": [], "timezone": "Etc/UTC"}

    def test_separate_export_wizard_never_asks_api_key_or_restarts_core(self):
        facts = self.installed_core()
        original = (self.root / ".env").read_bytes()
        with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "request_json", return_value={"status": "ready"}), patch.object(integrate, "run") as run, patch("builtins.input", side_effect=["native", "123", "", "", "", "apply"]), patch("getpass.getpass") as secret:
            self.assertEqual(integrate.integrate(self.root, "export"), 0)
        secret.assert_not_called(); run.assert_not_called()
        self.assertEqual((self.root / ".env").read_bytes(), original)

    def test_failed_network_change_restores_core_and_keeps_manual_export(self):
        facts = self.installed_core()
        original = (self.root / ".env").read_bytes()
        def topology(api, *args):
            api["STOLAS_LISTEN"] = "172.18.0.1"
            return dict(topology="docker", docker_id="abc", endpoint="http://172.18.0.1:8080")
        with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate, "collect_topology", side_effect=topology), patch.object(integrate, "validate_discovered_endpoint"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "check_connection", side_effect=RuntimeError("exception-private-secret")), patch.object(integrate, "run") as run, patch("builtins.input", side_effect=["123", "", "", "", "apply"]):
            self.assertEqual(integrate.integrate(self.root, "export"), 2)
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all("up" in call.args[0] for call in run.call_args_list))
        self.assertTrue((self.root / "n8n/local.json").exists())
        self.assertNotIn("exception-private-secret", self.output.getvalue())

    def test_real_http_metadata_and_workflow_boundary(self):
        received = []
        items = self.items
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append((self.path, None))
                self.reply({"data": items, "nextCursor": None})
            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.reply({"id": "created"})
            def reply(self, body):
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(body).encode())
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join); self.addCleanup(server.shutdown)
        self.options["url"] = f"http://127.0.0.1:{server.server_port}"
        with patch("builtins.input", return_value="connect"):
            self.assertTrue(integrate.connect_n8n(self.root, self.options, self.api, self.data))
        self.assertEqual([p for p, b in received], ["/api/v1/credentials?limit=100", "/api/v1/workflows"])


@unittest.skipUnless(HAS_TOOLS and os.getenv("STOLAS_N8N_INTEGRATION") == "1", "Opt-in isolated pinned n8n API integration")
class RealN8nTests(unittest.TestCase):
    def test_existing_credentials_and_inactive_workflow_with_real_n8n(self):
        name = "stolas-n8n-test-" + uuid.uuid4().hex[:12]
        image = "docker.n8n.io/n8nio/n8n:2.42.6@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a"
        def docker(*args):
            return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=120).stdout.strip()
        docker("run", "-d", "--name", name, "-p", "127.0.0.1::5678",
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
                with opener.open(base + "/healthz", timeout=2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail("isolated n8n failed to start")
                time.sleep(.5)
        # This is fixture setup in a disposable instance, never the user's n8n.
        rest("/rest/owner/setup", {"email": "ci@example.test", "firstName": "Stolas", "lastName": "CI", "password": "FixtureOnlyN8n123!"})
        key = rest("/rest/api-keys", {"label": "Stolas integration test", "scopes": ["credential:list", "credential:create", "workflow:create", "workflow:read"], "expiresAt": None})["rawApiKey"]
        options = dict(mode="api", endpoint="http://127.0.0.1:8080", url=base, key=key, chat_id="123", hours=3, timezone="Etc/UTC")
        for kind, values in (("telegramApi", {"accessToken": "123:fixture-not-a-real-bot", "baseUrl": "https://api.telegram.org"}), ("httpHeaderAuth", {"name": "Authorization", "value": "Bearer " + "a" * 40})):
            integrate.request_json(base + "/api/v1", "/credentials", key, {"name": "Existing " + kind, "type": kind, "data": values}, n8n=True)
        before = integrate.list_credentials(options)
        with tempfile.TemporaryDirectory() as directory, patch("builtins.input", return_value="connect"), patch("getpass.getpass") as secret:
            root = Path(directory)
            self.assertTrue(integrate.connect_n8n(root, options, {"STOLAS_API_TOKEN": "a" * 40}, integrate.workflow(ROOT, options)))
            state = json.loads((root / "n8n/install-state.json").read_text())
            workflow = integrate.request_json(base + "/api/v1", "/workflows/" + state["workflow_id"], key, n8n=True)
        secret.assert_not_called()
        self.assertFalse(workflow["active"])
        after = integrate.list_credentials(options)
        self.assertEqual({c["id"] for c in before}, {c["id"] for c in after})
        tg = next(c for c in before if c["type"] == "telegramApi")
        node = next(n for n in workflow["nodes"] if n["name"] == "Telegram alert")
        self.assertEqual(node["credentials"]["telegramApi"]["id"], tg["id"])
