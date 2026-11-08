"""Installer UX regression tests; all system discovery is mocked unless opted in."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/environment.py").exists()
if HAS_TOOLS:
    from tools import environment as env, install, integrate


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
        self.sudo_ok = False
        self.context = "unix:///var/run/docker.sock"

    def command(self, args):
        self.commands.append(args)
        output, error = "", ""
        if args[0] == "ip":
            output = json.dumps(self.addresses)
        elif "info" in args:
            output, error = "linux x86_64 28.0.0", "" if args[0] == "sudo" and self.sudo_ok else self.info_error
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


    def test_remote_docker_is_not_used_for_local_discovery(self):
        self.context = "ssh://remote-host"
        facts = self.discover()
        self.assertFalse(facts["docker"]["available"])
        self.assertIn("удалённый", facts["docker"]["reason"])

    def test_interrupt_preserves_existing_configuration_and_recovery_data(self):
        facts = self.discover()
        cfg = copy.deepcopy(install.DEFAULT)
        cfg["route"]["expected_public_cidrs"] = ["192.0.2.1/32"]
        saved = dict(config=cfg, api=dict(STOLAS_LISTEN="127.0.0.1", STOLAS_PORT="8080", STOLAS_API_TOKEN="a" * 40), completed=["wan"])
        files = {"config/local.json": json.dumps(cfg),
                 ".env": "STOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\nSTOLAS_API_TOKEN=" + "a" * 40,
                 ".stolas-draft.json": json.dumps(saved),
                 ".stolas-progress.json": '{"stage":"configured"}',
                 ".stolas-backups/restore/config/local.json": json.dumps(cfg),
                 "history.json": '[{"status":"ok"}]'}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        with patch("builtins.input", side_effect=["resume", "wan", "off", KeyboardInterrupt]), patch.object(env, "port_state", return_value="free"), self.assertRaises(KeyboardInterrupt):
            install.collect_plan(self.root, cfg, facts)
        for name, content in files.items():
            self.assertEqual((self.root / name).read_text(), content)

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
                for index, choice in enumerate(choices):
                    with socket.socket() as listener:
                        listener.bind((choice["address"], 0))
                        port = listener.getsockname()[1]
                    api = dict(STOLAS_API_TOKEN="a" * 40, STOLAS_LISTEN="127.0.0.1", STOLAS_PORT=str(port))
                    with patch("builtins.input", side_effect=["docker", str(index + 1), "", "trusted"]):
                        options = integrate.collect_topology(api, facts, root)
                    integrate.validate_discovered_endpoint(dict(api=api, n8n={**options, "mode": "export"}), facts, root)
                    self.assertEqual(api["STOLAS_LISTEN"], choice["address"])
                    server = make_server(Runner(copy.deepcopy(install.DEFAULT), Store(root / "data")), (api["STOLAS_LISTEN"], int(api["STOLAS_PORT"])), "a" * 40)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        integrate.check_connection(root, options, api, ["docker", "compose"])
                        with self.assertRaises(RuntimeError):
                            integrate.check_connection(root, options, {"STOLAS_API_TOKEN": "b" * 40}, ["docker", "compose"])
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
