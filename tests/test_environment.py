"""Installer UX regression tests; all system discovery is mocked unless opted in."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/environment.py").exists()
if HAS_TOOLS:
    from tools import environment as env, install, deploy


def fixtures(root):
    container = {"id": "abc", "name": "automation-app", "image": "docker.n8n.io/n8nio/n8n:2.42.6",
                 "command": ["start", "--fake-secret=never-disclose"], "running": True, "mode": "project_default",
                 "project": "automation", "service": "editor", "directory": "/foreign",
                 "networks": {"automation_default": {"NetworkID": "net1", "IPAddress": "172.18.0.2", "Gateway": "172.18.0.1"}}}
    network = {"id": "net1", "name": "automation_default", "driver": "bridge", "internal": False,
               "bridge": "br-test", "ipam": [{"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}]}
    addresses = [{"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
                 {"ifname": "br-test", "addr_info": [{"local": "172.18.0.1"}]}]
    return container, network, addresses


@unittest.skipUnless(HAS_TOOLS, "Installer sources are not in production image")
class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.container, self.network, self.addresses = fixtures(self.root)
        self.containers, self.networks = [self.container], [self.network]
        self.commands = []
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.info_error = ""
        self.context = "unix:///var/run/docker.sock"

    def command(self, args):
        self.commands.append(args)
        output, error = "", ""
        if args[0] == "ip":
            output = json.dumps(self.addresses)
        elif "info" in args:
            output, error = "linux x86_64 28.0.0", self.info_error
        elif "context" in args:
            output = self.context
        elif "compose" in args:
            output = "2.39.0"
        elif "ps" in args:
            output = " ".join(c["id"] for c in self.containers)
        elif "network" in args:
            output = "\n".join(json.dumps(n) for n in self.networks)
        elif "inspect" in args:
            output = "\n".join(json.dumps(c) for c in self.containers)
        return subprocess.CompletedProcess(args, 1 if error else 0, output, error)

    def discover(self, tools=("docker", "ip")):
        with patch.object(env, "command", side_effect=self.command), patch.object(env.shutil, "which", side_effect=lambda name: name if name in tools else None):
            return env.discover(self.root)

    def topology(self, answers=None, facts=None):
        api = {"STOLAS_LISTEN": "127.0.0.1", "STOLAS_PORT": "8080", "STOLAS_API_TOKEN": "a" * 40}
        with patch("builtins.input", side_effect=answers or ["docker"]) as prompt, patch.object(env, "port_state", return_value="free"):
            result = install.collect_topology(api, facts or self.discover(), self.root)
        return api, result, prompt.call_count

    def test_official_image_with_arbitrary_name_is_auto_selected(self):
        facts = self.discover()
        api, options, count = self.topology(facts=facts)
        self.assertEqual(count, 1)  # Intent only; no container, IP or endpoint input.
        self.assertEqual(api["STOLAS_LISTEN"], "172.18.0.1")
        self.assertEqual(options["endpoint"], "http://172.18.0.1:8080")
        self.assertEqual(options["docker_id"], "abc")
        self.assertNotIn("never-disclose", json.dumps(facts))
        self.assertNotIn(".Config.Env", env.CONTAINER_FORMAT)
        self.assertTrue(all("create" not in c and "connect" not in c and "rm" not in c for c in self.commands))

    def test_compose_metadata_detects_custom_image_and_requires_confirmation(self):
        self.container.update(image="private/automation-custom:v1", service="n8n", name="editor")
        _, result, count = self.topology(["docker", "1"])
        self.assertEqual(count, 2)
        self.assertEqual(result["docker_container"], "editor")

    def test_workers_are_not_treated_as_editors(self):
        worker = copy.deepcopy(self.container)
        worker.update(id="worker-id", name="background", command=["worker"])
        self.containers.append(worker)
        self.assertEqual(len(self.discover()["n8n"]), 1)

    def test_n8n_compose_project_does_not_turn_database_into_n8n(self):
        self.container["project"] = "n8n"
        for name, image in (("n8n-db", "postgres:17"), ("n8n-redis", "redis:7"), ("proxy", "traefik:3")):
            companion = copy.deepcopy(self.container)
            companion.update(id=name, name=name, service=name, image=image, command=["redis-server", "--requirepass", "n8n"] if "redis" in name else [])
            self.containers.append(companion)
        self.assertEqual([c["name"] for c in self.discover()["n8n"]], ["automation-app"])

    def test_multiple_instances_offer_named_choices(self):
        second = copy.deepcopy(self.container)
        second.update(id="second", name="other-editor", project="other-project")
        self.containers.append(second)
        _, result, count = self.topology(["docker", "2"])
        self.assertEqual(result["docker_id"], "second")
        self.assertEqual(count, 2)
        self.assertIn("other-project", self.output.getvalue())

    def test_multiple_networks_offer_actual_interfaces(self):
        self.container["networks"]["secondary"] = {"NetworkID": "net2", "Gateway": "172.19.0.1", "IPAddress": "172.19.0.2"}
        self.networks.append(dict(self.network, id="net2", name="secondary", ipam=[{"Gateway": "172.19.0.1"}]))
        self.addresses.append({"ifname": "br-secondary", "addr_info": [{"local": "172.19.0.1"}]})
        api, options, count = self.topology(["docker", "2"])
        self.assertEqual(count, 2)
        self.assertEqual(api["STOLAS_LISTEN"], "172.19.0.1")
        self.assertEqual(options["network"], "secondary")
        self.assertIn("br-secondary", self.output.getvalue())

    def test_docker_missing_daemon_down_and_permissions_are_distinct(self):
        facts = self.discover(tools=("ip",))
        self.assertIn("не установлен", facts["docker"]["reason"])
        self.info_error = "Cannot connect to Docker daemon"
        self.assertIn("API недоступен", self.discover()["docker"]["reason"])
        self.info_error = "permission denied while trying to connect"
        self.assertIn("прав к Docker socket", self.discover(tools=("docker", "ip", "sudo"))["docker"]["reason"])
        self.assertIn(["sudo", "-n", "docker"], [c[:3] for c in self.commands])

    def test_remote_docker_is_not_used_for_local_discovery(self):
        self.context = "ssh://remote-host"
        facts = self.discover()
        self.assertFalse(facts["docker"]["available"])
        self.assertIn("удалённый", facts["docker"]["reason"])

    def test_missing_gateway_gateway_not_local_and_ipam_conflict(self):
        attachment = self.container["networks"]["automation_default"]
        for value, text in (("", "отсутствует"), ("172.22.0.1", "противоречит"), ("172.18.0.1", "интерфейсах")):
            attachment["Gateway"] = value
            self.addresses = self.addresses[:1]
            facts = self.discover()
            choices, reasons = env.host_candidates(facts["n8n"][0], facts)
            self.assertEqual(choices, [])
            self.assertIn(text, reasons[0])
        with self.assertRaisesRegex(ValueError, "подтверждённый адрес"):
            self.topology(facts=facts)

    def test_macvlan_never_treats_router_as_host_gateway(self):
        self.network["driver"] = "macvlan"
        with self.assertRaisesRegex(ValueError, "macvlan"):
            self.topology()

    def test_host_network_uses_loopback_without_gateway_prompt(self):
        self.container.update(mode="host", networks={})
        api, options, count = self.topology()
        self.assertEqual(options["endpoint"], "http://127.0.0.1:8080")
        self.assertEqual(count, 1)
        self.assertEqual(api["STOLAS_LISTEN"], "127.0.0.1")

    def test_busy_port_selects_free_port_and_recomputes_endpoint(self):
        api = {"STOLAS_LISTEN": "127.0.0.1", "STOLAS_PORT": "8080"}
        with patch("builtins.input", side_effect=["docker"]), patch.object(env, "port_state", side_effect=["busy", "free"]):
            result = install.collect_topology(api, self.discover(), self.root)
        self.assertEqual(result["endpoint"], "http://172.18.0.1:8081")
        self.assertIn("8080 занят", self.output.getvalue())

    def test_existing_authenticated_stolas_port_is_reused(self):
        (self.root / ".env").write_text("STOLAS_LISTEN=172.18.0.1\nSTOLAS_PORT=8080\nSTOLAS_API_TOKEN=" + "a" * 40)
        facts = self.discover()
        facts["stolas"] = [{"id": "owned"}]
        api = install.read_api(self.root)
        with patch.object(env, "port_state", return_value="busy"), patch.object(install, "request_json", return_value={"status": "ready"}) as request:
            install.propose_port(api, facts, self.root)
        self.assertEqual(api["STOLAS_PORT"], "8080")
        request.assert_called_once()

    def test_remote_https_is_user_information_and_never_public_bind(self):
        api, result, count = self.topology(["lan", "https://monitor.example.test"])
        self.assertEqual(api["STOLAS_LISTEN"], "127.0.0.1")
        self.assertEqual(result["endpoint"], "https://monitor.example.test")

    def test_standard_wizard_never_asks_gateway_ip_or_computed_url(self):
        facts = self.discover()
        answers = ["", "192.0.2.1/32", "export", "", "12345", "", "apply"]
        with patch("builtins.input", side_effect=answers) as prompt, patch.object(env, "port_state", return_value="free"):
            plan = install.collect_plan(self.root, copy.deepcopy(install.DEFAULT), facts)
        self.assertEqual(prompt.call_count, len(answers))
        self.assertEqual(plan["n8n"]["endpoint"], "http://172.18.0.1:8080")
        prompts = "\n".join(call.args[0] for call in prompt.call_args_list)
        self.assertNotIn("gateway", prompts.lower())
        self.assertNotIn("URL", prompts)
        self.assertNotIn("IPv4 хоста", prompts)
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / "config/local.json").exists())
        self.assertNotIn(plan["api"]["STOLAS_API_TOKEN"], self.output.getvalue())

    def test_cancel_and_back_keep_live_configuration_untouched(self):
        facts = self.discover()
        answers = ["", "192.0.2.1/32", ":back", "off", "later", "api", ":back", "cancel"]
        with patch("builtins.input", side_effect=answers), patch.object(env, "port_state", return_value="free"), self.assertRaises(install.Cancel):
            install.collect_plan(self.root, copy.deepcopy(install.DEFAULT), facts)
        self.assertFalse((self.root / ".env").exists())
        self.assertFalse((self.root / ".stolas-draft.json").exists())

    def test_interrupt_and_resume_preserve_token_and_finished_wan_step(self):
        facts = self.discover()
        with patch("builtins.input", side_effect=["", "192.0.2.1/32", KeyboardInterrupt]), self.assertRaises(KeyboardInterrupt):
            install.collect_plan(self.root, copy.deepcopy(install.DEFAULT), facts)
        draft = json.loads((self.root / ".stolas-draft.json").read_text())
        self.assertEqual(draft["completed"], ["wan"])
        if os.name != "nt":
            self.assertEqual((self.root / ".stolas-draft.json").stat().st_mode & 0o777, 0o600)
        with patch("builtins.input", side_effect=["resume", "later", "apply"]) as prompt, patch.object(env, "port_state", return_value="free"):
            plan = install.collect_plan(self.root, copy.deepcopy(install.DEFAULT), facts)
        self.assertEqual(plan["api"]["STOLAS_API_TOKEN"], draft["api"]["STOLAS_API_TOKEN"])
        self.assertEqual(prompt.call_count, 3)

    def test_existing_configuration_opens_summary_without_reasking_fields(self):
        facts = self.discover()
        (self.root / "config").mkdir()
        cfg = copy.deepcopy(install.DEFAULT)
        cfg["route"]["expected_public_cidrs"] = ["192.0.2.1/32"]
        (self.root / "config/local.json").write_text(json.dumps(cfg))
        (self.root / ".env").write_text("STOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\nSTOLAS_API_TOKEN=" + "a" * 40)
        with patch("builtins.input", side_effect=["apply"]) as prompt, patch.object(env, "port_state", return_value="free"):
            plan = install.collect_plan(self.root, cfg, facts)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(plan["api"]["STOLAS_API_TOKEN"], "a" * 40)

    def test_invalid_input_explains_expected_range_and_secret_is_redacted(self):
        with patch("builtins.input", side_effect=["zero", "70000", "8080"]):
            self.assertEqual(install.ask("Порт Stolas", 8080, install.integer(1024, 65535)), 8080)
        self.assertIn("целое число", self.output.getvalue())
        self.assertIn("от 1024 до 65535", self.output.getvalue())
        with patch.object(install.getpass, "getpass", side_effect=["my-sensitive-value", "a" * 40]):
            install.ask("Токен", "", install.matching(r"[a-z]{40}"), secret=True)
        self.assertNotIn("my-sensitive-value", self.output.getvalue())

    def test_network_probe_failure_is_not_replaced_with_host_success(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "")), patch.object(install, "request_json") as host, self.assertRaisesRegex(RuntimeError, "не достигает"):
            install.check_connection(self.root, {"topology": "docker", "docker_id": "abc", "endpoint": "http://172.18.0.1:8080"}, {"STOLAS_API_TOKEN": "a" * 40}, ["docker", "compose"])
        host.assert_not_called()

    def test_diagnosis_does_not_create_target_or_lock(self):
        target = self.root / "absent"
        with patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, patch("sys.argv", ["deploy", "--target", str(target), "--diagnose"]):
            self.assertEqual(deploy.main(), 0)
        self.assertIn("--diagnose", run.call_args.args[0])
        self.assertFalse(target.exists())

    def test_gateway_change_after_rescan_blocks_stale_endpoint(self):
        facts = self.discover()
        api, options, _ = self.topology(facts=facts)
        choices = {"config": copy.deepcopy(install.DEFAULT), "api": api, "n8n": {**options, "mode": "keep"}}
        facts["addresses"] = facts["addresses"][:1]
        with self.assertRaisesRegex(ValueError, "endpoint"):
            install.validate_discovered_endpoint(choices, facts)

    def test_api_key_checkpoint_survives_interrupt_before_bot_token(self):
        facts = self.discover()
        with patch("builtins.input", side_effect=["", "192.0.2.1/32", "api", "", "12345", "", "https://n8n.example.test"]), patch.object(install.getpass, "getpass", side_effect=["remembered-private-key", KeyboardInterrupt]), patch.object(env, "port_state", return_value="free"), self.assertRaises(KeyboardInterrupt):
            install.collect_plan(self.root, copy.deepcopy(install.DEFAULT), facts)
        draft = json.loads((self.root / ".stolas-draft.json").read_text())
        self.assertEqual(draft["n8n"]["key"], "remembered-private-key")
        with patch("builtins.input", side_effect=["resume", "", "", "apply"]), patch.object(install.getpass, "getpass", return_value="123:bot_token") as secret, patch.object(env, "port_state", return_value="free"):
            plan = install.collect_plan(self.root, copy.deepcopy(install.DEFAULT), facts)
        self.assertEqual(secret.call_count, 1)  # Only the missing bot token.
        self.assertEqual(plan["n8n"]["key"], "remembered-private-key")
        self.assertNotIn("remembered-private-key", self.output.getvalue())

    def test_port_race_requires_review_of_recomputed_url(self):
        facts = self.discover()
        answers = ["", "192.0.2.1/32", "export", "", "12345", "", "apply", "apply"]
        with patch("builtins.input", side_effect=answers), patch.object(env, "port_state", side_effect=["free", "free", "busy", "free", "free"]):
            plan = install.collect_plan(self.root, copy.deepcopy(install.DEFAULT), facts)
        self.assertEqual(plan["n8n"]["endpoint"], "http://172.18.0.1:8081")
        self.assertIn("сводку ещё раз", self.output.getvalue())

    def test_partial_old_checkout_runs_wizard_instead_of_reuse(self):
        source, target = self.root / "source", self.root / "target"
        for root in (source, target):
            for name in ("install.sh", "tools/install.py", "agent/config.py", "compose.yaml"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# fixture")
        with patch.object(deploy, "run_installer", return_value=0) as run:
            self.assertEqual(deploy.deploy(source, target, ref="v0.3.0", action="update"), 0)
        self.assertFalse(run.call_args.kwargs["reuse"])

    def test_cancelled_first_install_can_be_retried_without_removing_directory(self):
        source, target = self.root / "source", self.root / "target"
        for name in ("install.sh", "tools/install.py", "agent/config.py", "compose.yaml"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture")
        with patch.object(deploy, "run_installer", return_value=3):
            self.assertEqual(deploy.deploy(source, target), 3)
        self.assertFalse((target / "install.sh").exists())
        with patch.object(deploy, "run_installer", return_value=0):
            self.assertEqual(deploy.deploy(source, target), 0)

    def test_n8n_pending_create_never_repeats_and_known_credential_is_reused(self):
        (self.root / "n8n").mkdir()
        options = dict(mode="api", endpoint="https://stolas.example.test", url="https://n8n.example.test", key="private", bot_token="123:private")
        state_path = self.root / "n8n/install-state.json"
        state = {"url": options["url"], "credentials": [{"type": "httpHeaderAuth", "id": "known", "name": "Stolas API"}]}
        state_path.write_text(json.dumps(state))
        calls = []
        def request(base, path, token, body=None, n8n=False):
            if body:
                calls.append((path, body))
                return {"id": "new-" + str(len(calls))}
            return {"status": "ready", "properties": {"name": {}, "value": {}, "accessToken": {}}}
        data = {"nodes": [{"name": name} for name in ("Run Stolas", "Read summary", "Telegram alert")], "name": "test", "settings": {}, "connections": {}}
        with patch.object(install, "request_json", side_effect=request):
            install.connect_n8n(self.root, options, {"STOLAS_API_TOKEN": "a" * 40}, data)
        self.assertEqual([p for p, body in calls], ["/credentials", "/workflows"])
        self.assertEqual(calls[0][1]["type"], "telegramApi")
        state_path.write_text(json.dumps({**state, "pending": "telegramApi"}))
        with patch.object(install, "request_json", side_effect=request), self.assertRaisesRegex(RuntimeError, "мог создать"):
            install.connect_n8n(self.root, options, {"STOLAS_API_TOKEN": "a" * 40}, data)
        self.assertEqual(len(calls), 2)


@unittest.skipUnless(HAS_TOOLS and os.environ.get("STOLAS_DOCKER_INTEGRATION") == "1", "Opt-in isolated Linux Docker integration")
class DockerNetworkIntegrationTests(unittest.TestCase):
    def test_actual_gateway_and_authenticated_access_from_two_container_networks(self):
        from agent.api import make_server
        from agent.runner import Runner
        from agent.storage import Store
        identifier = "stolas-ux-ci-" + uuid.uuid4().hex[:10]
        image = "docker.n8n.io/n8nio/n8n:2.42.6@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a"
        def docker(*args):
            return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=60).stdout.strip()
        networks = []
        container = None
        try:
            for suffix in ("one", "two"):
                networks.append(docker("network", "create", identifier + "-" + suffix))
            container = docker("run", "-d", "--name", identifier, "--network", networks[0], "--entrypoint", "node", image, "-e", "setInterval(()=>{},1000)")
            docker("network", "connect", networks[1], container)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                facts = env.discover(root)
                selected = next(c for c in facts["n8n"] if c["id"] == container)
                choices, reasons = env.host_candidates(selected, facts)
                self.assertEqual(len(choices), 2, reasons)
                for choice in choices:
                    server = make_server(Runner(copy.deepcopy(install.DEFAULT), Store(root / "data")), (choice["address"], 0), "a" * 40)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        options = {"topology": "docker", "docker_id": container, "endpoint": f"http://{choice['address']}:{server.server_port}"}
                        install.check_connection(root, options, {"STOLAS_API_TOKEN": "a" * 40}, ["docker", "compose"])
                        with self.assertRaises(RuntimeError):
                            install.check_connection(root, options, {"STOLAS_API_TOKEN": "b" * 40}, ["docker", "compose"])
                    finally:
                        server.shutdown()
                        server.server_close()
                        thread.join()
        finally:
            if container:
                docker("rm", "-f", container)
            for network in reversed(networks):
                docker("network", "rm", network)


if __name__ == "__main__":
    unittest.main()
