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
        self.projects = [dict(id="p1", name="Personal project")]
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
            return {"data": self.projects if path.startswith("/projects?") else self.items, "nextCursor": None}
        return {"id": "new-id"}

    def connect(self, answers=("1", "connect")):
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
        self.assertTrue(self.connect(("2", "1", "connect")))
        body = next(b for p, b in self.calls if b)
        telegram = next(n for n in body["nodes"] if n["name"] == "Telegram alert")
        self.assertEqual(telegram["credentials"]["telegramApi"]["id"], "tg2")
        self.assertIn("tg2 credential", self.output.getvalue())

    def test_single_unknown_header_requires_explicit_selection(self):
        self.assertTrue(self.connect(("1", "connect")))
        self.assertIn("Credential доступа к Stolas — номер варианта", self.output.getvalue())

    def test_owned_header_id_can_be_reused_without_selection(self):
        (self.root / "n8n/install-state.json").write_text(json.dumps({"url": self.options["url"], "credentials": [{"id": "api", "type": "httpHeaderAuth"}]}))
        self.assertTrue(self.connect(("connect",)))
        self.assertNotIn("Credential доступа к Stolas — номер варианта", self.output.getvalue())

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

    def test_flat_public_api_projects_are_used_for_workflow_binding(self):
        for item in self.items:
            item["shared"] = [dict(id="p1", name="Personal project", role="credential:owner")]
        self.assertTrue(self.connect())
        payload = next(body for path, body in self.calls if path == "/workflows")
        self.assertEqual(payload["projectId"], "p1")
        self.assertIn("Personal project", self.output.getvalue())

    def test_project_mismatch_or_unknown_sharing_does_not_post(self):
        self.items[1] = self.credential("api", "httpHeaderAuth", "p2")
        self.assertFalse(self.connect(("1", "export")))
        self.assertFalse(any(b for p, b in self.calls))
        self.items[1].pop("shared")
        self.assertFalse(self.connect(("export",)))
        self.assertFalse(any(b for p, b in self.calls))
        for item in self.items:
            item.pop("shared", None)
        self.assertFalse(self.connect(("1",)))

    def test_selected_project_never_displays_foreign_credential_names(self):
        self.items.extend([self.credential("FOREIGN_TELEGRAM", "telegramApi", "p2"), self.credential("FOREIGN_API", "httpHeaderAuth", "p2")])
        self.assertTrue(self.connect(("1", "connect")))
        self.assertNotIn("FOREIGN_TELEGRAM", self.output.getvalue())
        self.assertNotIn("FOREIGN_API", self.output.getvalue())
        body = next(body for path, body in self.calls if path == "/workflows")
        self.assertEqual(body["projectId"], "p1")

    def test_projects_paginate_independently_of_credentials(self):
        pages = [{"data": self.projects, "nextCursor": "next/page"},
                 {"data": [dict(id="empty", name="Empty project")], "nextCursor": None}]
        with patch.object(integrate, "request_json", side_effect=pages) as request:
            projects = integrate.list_projects(self.options)
        self.assertEqual([p["id"] for p in projects], ["p1", "empty"])
        self.assertEqual(request.call_args.args[1], "/projects?limit=100&cursor=next%2Fpage")
        with patch.object(integrate, "request_json", return_value=pages[0]), self.assertRaises(ValueError):
            integrate.list_projects(self.options)
        with patch.object(integrate, "request_json", return_value={}), self.assertRaises(ValueError):
            integrate.list_projects(self.options)

    def test_empty_project_is_selectable_without_falling_back_to_another_project(self):
        self.projects.append(dict(id="empty", name="Empty project"))
        self.assertFalse(self.connect(("2",)))
        self.assertIn("Empty project", self.output.getvalue())
        self.assertNotIn("tg credential", self.output.getvalue())
        self.assertFalse(any(body for path, body in self.calls))
        self.assertEqual([path for path, body in self.calls],
                         ["/projects?limit=100", "/credentials?limit=100"])

    def test_numbered_project_selection_requires_a_valid_choice(self):
        self.projects.insert(0, dict(id="empty", name="Empty project"))
        self.items.extend([self.credential("other", "telegramApi", "empty"),
                           self.credential("wrong_type", "postgres")])
        self.assertTrue(self.connect(("", "3", "2", "1", "connect")))
        payload = next(body for path, body in self.calls if path == "/workflows")
        self.assertEqual(payload["projectId"], "p1")
        self.assertNotIn("other credential", self.output.getvalue())
        self.assertNotIn("wrong_type", self.output.getvalue())

    def test_shared_credential_is_available_in_selected_project(self):
        self.projects = [dict(id="p2", name="Shared project")]
        for item in self.items:
            item["shared"].append(dict(projectId="p2", role="credential:user"))
        self.assertTrue(self.connect())
        payload = next(body for path, body in self.calls if path == "/workflows")
        self.assertEqual(payload["projectId"], "p2")

    def test_matching_project_name_or_untyped_relation_id_is_not_access(self):
        for shared in ([dict(name="Personal project")], [dict(id="p1", name="Personal project")]):
            with self.subTest(shared=shared):
                self.items[0]["shared"] = shared
                self.assertFalse(self.connect(()))
        self.assertFalse(any(body for path, body in self.calls))

    def test_project_api_failure_stops_before_credentials_or_creation(self):
        with patch.object(integrate, "request_json", side_effect=integrate.ServiceRequestError("HTTP 403")) as request:
            self.assertFalse(integrate.connect_n8n(self.root, self.options, self.api, self.data))
        self.assertEqual(request.call_count, 1)
        text = " ".join(self.output.getvalue().split())
        self.assertIn("проектов: HTTP 403", text)
        self.assertIn("ручного импорта", text)

    def test_final_output_has_three_lines_and_one_plain_workflow_url(self):
        self.assertTrue(self.connect())
        text = self.output.getvalue()
        self.assertTrue(text.endswith("Готово: workflow создан в n8n.\nСтатус: неактивен.\nАдрес: https://n8n.example.test/workflow/new-id\n"))
        self.assertEqual(text.count("https://n8n.example.test/workflow/new-id"), 1)
        self.assertNotIn("Manual test", text)
        self.assertNotIn("Выберите Header Auth", text)
        self.assertNotIn("[https://", text)

    def test_host_timezone_change_exports_existing_workflow_without_remote_mutation(self):
        self.assertTrue(self.connect())
        self.calls.clear()
        self.options["timezone"] = "Europe/Moscow"
        self.data = integrate.workflow(ROOT, self.options)
        self.assertFalse(self.connect(()))
        self.assertEqual(self.calls, [])
        saved = json.loads((self.root / "n8n/local.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["settings"]["timezone"], "Europe/Moscow")
        self.assertIn("ручного обновления", " ".join(self.output.getvalue().split()))
        self.assertIn("/workflow/new-id", self.output.getvalue())

    def test_unknown_host_timezone_stops_before_wizard_or_writes(self):
        facts = self.installed_core()
        facts["timezone"] = None
        facts["timezone_error"] = "Не удалось определить пояс ОС"
        with patch.object(integrate.environment, "discover", return_value=facts), patch("builtins.input") as prompt, self.assertRaisesRegex(ValueError, "пояс ОС"):
            integrate.integrate(self.root, "export")
        prompt.assert_not_called()
        self.assertFalse((self.root / "n8n/local.json").exists())

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

    def test_workflow_propagates_selected_timezone_to_summary_formatter(self):
        options = {**self.options, "timezone": "Europe/Moscow", "notification_mode": "daily_summary"}
        workflow = integrate.workflow(ROOT, options)
        nodes = {node["name"]: node for node in workflow["nodes"]}
        self.assertEqual(workflow["settings"]["timezone"], "Europe/Moscow")
        self.assertFalse(nodes["Daily summary"]["disabled"])
        for name in ("Settings", "Summary settings"):
            settings_code = nodes[name]["parameters"]["jsCode"]
            self.assertIn('"timezone": "Europe/Moscow"', settings_code)
        self.assertIn("Суточный отчёт", nodes["Format summary"]["parameters"]["jsCode"])

    def test_generator_keeps_current_russian_summary_and_timezone_formatters(self):
        from tools.build_workflow import build
        self.assertEqual(build({"timezone": "Europe/Moscow"})["settings"]["timezone"], "Europe/Moscow")
        template = json.loads((ROOT / "n8n/stolas.json").read_text(encoding="utf-8"))
        generated = {n["name"]: n for n in build()["nodes"]}
        for node in template["nodes"]:
            if node["name"] in ("Classify result", "Format summary"):
                self.assertEqual(generated[node["name"]]["parameters"]["jsCode"], node["parameters"]["jsCode"])

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
        facts["timezone"] = "Europe/Moscow"
        (self.root / "n8n/settings.json").write_text(json.dumps({"timezone": "Europe/Berlin"}))
        original = (self.root / ".env").read_bytes()
        with patch.dict(os.environ, {"TZ": "Etc/UTC"}), patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "request_json", return_value={"status": "ready"}), patch.object(integrate, "run") as run, patch("builtins.input", side_effect=["native", "123", "", "", "apply"]) as prompt, patch("getpass.getpass") as secret:
            self.assertEqual(integrate.integrate(self.root, "export"), 0)
        secret.assert_not_called(); run.assert_not_called()
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertFalse(any("Часовой пояс" in c.args[0] for c in prompt.call_args_list))
        exported = json.loads((self.root / "n8n/local.json").read_text(encoding="utf-8"))
        self.assertEqual(exported["settings"]["timezone"], "Europe/Moscow")
        for node in exported["nodes"]:
            if node["name"] in ("Settings", "Summary settings"):
                self.assertIn('"timezone": "Europe/Moscow"', node["parameters"]["jsCode"])

    def test_cancel_before_apply_does_not_request_key_or_change_configuration(self):
        facts = self.installed_core()
        original = (self.root / ".env").read_bytes()
        with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "request_json") as request, patch.object(integrate, "run") as run, patch("builtins.input", side_effect=["native", "123", "", "", "cancel"]):
            with self.assertRaises(integrate.Cancel):
                integrate.integrate(self.root, "export")
        request.assert_not_called()
        run.assert_not_called()
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertFalse((self.root / "n8n/local.json").exists())

    def test_safe_connection_diagnostic_is_shown_instead_of_credential_permission_claim(self):
        with patch.object(integrate, "request_json", side_effect=integrate.ServiceRequestError("Сертификат HTTPS не прошёл проверку.")):
            self.assertFalse(integrate.connect_n8n(self.root, self.options, self.api, self.data))
        self.assertIn("Сертификат HTTPS не прошёл проверку", " ".join(self.output.getvalue().split()))
        self.assertNotIn("не разрешил чтение", self.output.getvalue())

    def test_wizard_preserves_n8n_address_without_transport_confirmation(self):
        facts = self.installed_core()
        for address in ("http://192.168.50.20:5678/api", "http://n8n.example.test:5678", "http://203.0.113.20:5678", "https://n8n.example.test/api"):
            with self.subTest(address=address):
                answers = ["native", "123", "", "", address]
                answers += ["fixture-key", "apply"]
                with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "check_connection"), patch.object(integrate, "connect_n8n", return_value=True) as connect, patch.object(integrate, "run") as run, patch("builtins.input", side_effect=answers):
                    self.assertEqual(integrate.integrate(self.root, "api"), 0)
                self.assertEqual(connect.call_args.args[1]["url"], address)
                saved = (self.root / "n8n/settings.json").read_text()
                self.assertEqual(json.loads(saved)["url"], address)
                self.assertNotIn("fixture-key", saved)
                run.assert_not_called()

    def test_complete_api_wizard_finishes_without_manual_instructions(self):
        facts = self.installed_core()
        answers = ["native", "123", "", "", self.options["url"], "fixture-key", "apply", "1", "connect"]
        with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "check_connection"), patch.object(integrate, "request_json", side_effect=self.request), patch.object(integrate, "run") as run, patch("builtins.input", side_effect=answers) as prompt:
            self.assertEqual(integrate.integrate(self.root, "api"), 0)
        run.assert_not_called()
        text = self.output.getvalue()
        self.assertTrue(text.endswith("Статус: неактивен.\nАдрес: https://n8n.example.test/workflow/new-id\n"))
        for removed in ("Выберите Header Auth", "Manual test", "Продолжить с указанным HTTP", "reverse proxy", "X-N8N-API-KEY", "Импорт:"):
            self.assertNotIn(removed, text)
        self.assertEqual(prompt.call_count, len(answers))

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

    def test_real_http_metadata_and_workflow_boundary(self):
        received = []
        items = self.items
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append((self.path, None))
                self.reply({"data": [dict(id="p1", name="Project")] if self.path.startswith("/api/v1/projects?") else items, "nextCursor": None})
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
        with patch("builtins.input", side_effect=["1", "connect"]):
            self.assertTrue(integrate.connect_n8n(self.root, self.options, self.api, self.data))
        self.assertEqual([p for p, b in received], ["/api/v1/projects?limit=100", "/api/v1/credentials?limit=100", "/api/v1/workflows"])


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
        key = rest("/rest/api-keys", {"label": "Stolas integration test", "scopes": ["project:list", "credential:list", "credential:create", "workflow:create", "workflow:read"], "expiresAt": None})["rawApiKey"]
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
            answers = ["native", "123", "", "", base, key, "apply", "1", "connect"]
            with patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch.object(integrate, "docker_command", return_value=["docker", "compose"]), patch.object(integrate, "check_connection"), patch("builtins.input", side_effect=answers):
                self.assertEqual(integrate.integrate(root, "api"), 0)
            saved = json.loads((root / "n8n/settings.json").read_text())
            self.assertEqual(saved["url"], base)
            self.assertNotIn("http_trusted", saved)
            self.assertNotIn("n8n_http_trusted", saved)
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
