"""Placement and listener fixtures are synthetic; no site network is configured."""
import contextlib
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/integrate.py").is_file()
if HAS_TOOLS:
    from tools import integrate, install


@unittest.skipUnless(HAS_TOOLS, "Integration wizard is not shipped in Core image")
class PlacementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.api = dict(STOLAS_LISTEN="127.0.0.1", STOLAS_PORT="8080", STOLAS_API_TOKEN="a" * 40)
        self.facts = dict(docker={"available": True}, stolas=[], n8n=[], networks={}, addresses=[
            dict(address="127.0.0.1", interface="lo"), dict(address="192.168.1.10", interface="eth0"),
            dict(address="10.8.0.1", interface="wg0"), dict(address="172.18.0.1", interface="br-test")])
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def collect(self, answers, previous=None, state="free"):
        with patch("builtins.input", side_effect=answers), patch.object(integrate.environment, "port_state", return_value=state):
            return integrate.collect_topology(self.api, self.facts, self.root, previous)

    def test_exactly_three_placement_choices_and_native_default(self):
        options = self.collect(["native", "", "trusted"])
        self.assertEqual(options["endpoint"], "http://127.0.0.1:8080")
        text = self.output.getvalue()
        self.assertIn("1. n8n в Docker", text)
        self.assertIn("2. n8n без Docker", text)
        self.assertIn("3. n8n на другом хосте", text)
        self.assertNotIn("4.", text)
        self.assertEqual(self.api["STOLAS_LISTEN"], "127.0.0.1")

    def test_remote_lan_and_overlay_are_equal_placement_choices(self):
        for selected, address in (("1", "10.8.0.1"), ("2", "192.168.1.10")):
            with self.subTest(address=address):
                options = self.collect(["remote", selected, "", "trusted"])
                self.assertEqual(options["topology"], "remote")
                self.assertEqual(self.api["STOLAS_LISTEN"], address)
                self.assertEqual(options["endpoint"], "http://" + address + ":8080")
                self.assertTrue(options["http_trusted"])
                self.assertFalse(options["endpoint_override"])
        self.assertIn("Authorization: Bearer", self.output.getvalue())

    def test_remote_addresses_are_deterministic_and_bridge_is_last(self):
        expected = integrate.local_listener_candidates(self.facts)
        self.facts["addresses"].reverse()
        self.assertEqual(integrate.local_listener_candidates(self.facts), expected)
        self.assertEqual([item["address"] for item in expected], ["10.8.0.1", "192.168.1.10", "127.0.0.1", "172.18.0.1"])

    def test_manual_existing_local_address_and_separate_port(self):
        options = self.collect(["remote", "5", "192.168.1.10:9090", "", "trusted"])
        self.assertEqual(self.api["STOLAS_PORT"], "9090")
        self.assertEqual(self.api["STOLAS_LISTEN"], "192.168.1.10")
        self.assertEqual(options["endpoint"], "http://192.168.1.10:9090")

    def test_manual_binding_works_without_iproute_discovery(self):
        self.facts["addresses"] = []
        options = self.collect(["remote", "1", "192.168.1.10:8080", "", "trusted"])
        self.assertEqual(options["endpoint"], "http://192.168.1.10:8080")

    def test_loopback_proxy_override_does_not_change_listener(self):
        options = self.collect(["remote", "3", "https://stolas.example.test/api"])
        self.assertEqual(self.api["STOLAS_LISTEN"], "127.0.0.1")
        self.assertEqual(options["endpoint"], "https://stolas.example.test/api")
        self.assertTrue(options["endpoint_override"])

    def test_http_hostname_override_requires_explicit_network_trust(self):
        with self.assertRaises(install.Cancel):
            self.collect(["native", "http://stolas.example.test:8080", "cancel"])
        options = self.collect(["native", "http://stolas.example.test:8080", "trusted"])
        self.assertTrue(options["http_trusted"])

    def test_wildcard_requires_separate_confirmation_and_a_client_endpoint(self):
        with self.assertRaises(install.Cancel):
            self.collect(["remote", "5", "0.0.0.0:8080", "cancel"])
        options = self.collect(["remote", "5", "0.0.0.0:8080", "allow_bind", "https://stolas.example.test"])
        self.assertEqual(self.api["STOLAS_BIND_CONFIRMED"], "1")
        self.assertEqual(self.api["STOLAS_LISTEN"], "0.0.0.0")
        self.assertEqual(options["endpoint"], "https://stolas.example.test")
        cfg = copy.deepcopy(install.DEFAULT)
        cfg["route"]["mode"] = "off"
        install.validate_plan(dict(api=self.api, config=cfg))
        self.api.pop("STOLAS_BIND_CONFIRMED")
        with self.assertRaises(ValueError):
            install.validate_plan(dict(api=self.api, config=cfg))

    def test_invalid_listener_addresses_and_ports(self):
        multicast = str(integrate.ipaddress.IPv4Address(0xE0000001)) + ":8080"
        broadcast = str(integrate.ipaddress.IPv4Address(0xFFFFFFFF)) + ":8080"
        for value in ("example.test:8080", "192.168.1.10:0", "192.168.1.10:65536", multicast, broadcast, "127.0.0.1", "[::1]:8080"):
            with self.subTest(value=value), self.assertRaises(integrate.BindingError):
                integrate.listener(value)
        for state, diagnostic in (("not_local", "отсутствует"), ("busy", "занят"), ("denied", "привязку")):
            with self.subTest(state=state), self.assertRaisesRegex(integrate.BindingError, diagnostic):
                self.collect(["remote", "5", "192.168.1.99:8080"], state=state)

    def test_real_os_rejects_nonlocal_manual_binding(self):
        # Documentation prefix is not configured on the isolated test host.
        api = {**self.api, "STOLAS_LISTEN": "192.0.2.254", "STOLAS_PORT": "54321"}
        with self.assertRaisesRegex(integrate.BindingError, "отсутствует"):
            integrate.validate_listener(api, self.facts, self.root)

    def test_legacy_transport_values_preserve_listener_with_confirmation(self):
        for topology in ("lan", "vpn", "proxy"):
            with self.subTest(topology=topology):
                self.api.update(STOLAS_LISTEN="10.8.0.1", STOLAS_PORT="9090")
                options = self.collect(["remote", "keep", ""], dict(topology=topology, endpoint="https://stolas.example.test"))
                self.assertEqual(self.api["STOLAS_LISTEN"], "10.8.0.1")
                self.assertEqual(self.api["STOLAS_PORT"], "9090")
                self.assertEqual(options["topology"], "remote")
                self.assertEqual(options["endpoint"], "https://stolas.example.test")
        self.assertIn("Сохранить текущий listener", self.output.getvalue())

    def test_legacy_migration_can_cancel_before_any_file_changes(self):
        before = dict(self.api)
        with self.assertRaises(install.Cancel):
            self.collect(["remote", ":cancel"], dict(topology="vpn", endpoint="http://10.8.0.1:8080"))
        self.assertEqual(self.api, before)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_docker_listener_and_proxy_endpoint_validate_independently(self):
        self.facts["n8n"] = [dict(name="test-editor", id="test-id", image="n8nio/n8n:2", confidence="image", mode="bridge")]
        candidate = dict(network="test-network", interface="br-test", address="172.18.0.1")
        with patch.object(integrate.environment, "host_candidates", return_value=([candidate], [])):
            options = self.collect(["docker", "https://stolas.example.test"])
            with patch.object(integrate.environment, "port_state", return_value="free"):
                integrate.validate_discovered_endpoint(dict(api=self.api, n8n={**options, "mode": "export"}), self.facts, self.root)
        self.assertEqual(self.api["STOLAS_LISTEN"], "172.18.0.1")
        self.assertEqual(options["endpoint"], "https://stolas.example.test")
        self.assertEqual(options["docker_id"], "test-id")

    def test_local_bind_failure_and_endpoint_failure_have_distinct_diagnostics(self):
        options = dict(topology="remote", endpoint="https://stolas.example.test")
        with patch.object(integrate, "request_json", side_effect=RuntimeError("private-secret")):
            with self.assertRaisesRegex(integrate.IntegrationConnectionError, "Локальный listener") as error:
                integrate.check_connection(self.root, options, self.api, ["docker", "compose"])
        self.assertNotIn("private-secret", str(error.exception))
        with patch.object(integrate, "request_json", side_effect=[dict(status="ready"), RuntimeError("private-secret")]):
            with self.assertRaisesRegex(integrate.IntegrationConnectionError, "Endpoint n8n недоступен"):
                integrate.check_connection(self.root, options, self.api, ["docker", "compose"])

    def test_wildcard_local_health_check_uses_loopback(self):
        self.api["STOLAS_LISTEN"] = "0.0.0.0"
        with patch.object(integrate, "request_json", return_value=dict(status="ready")) as request:
            integrate.check_connection(self.root, dict(topology="remote", endpoint="https://stolas.example.test"), self.api, ["docker", "compose"])
        self.assertEqual(request.call_args_list[0].args[0], "http://127.0.0.1:8080")

