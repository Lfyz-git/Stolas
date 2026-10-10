import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent import events, probes
from agent.config import DEFAULT
from agent.runner import Runner
from agent.storage import Store


class WanRoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULT)
        self.cfg.update(min_interval=0, retry_delay=0)
        self.cfg["route"].update(expected_public_cidrs=["192.0.2.10/32"], min_confirmations=2)
        self.route = subprocess.CompletedProcess([], 0, '[{"dev":"eth0","gateway":"192.168.1.1"}]', "")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.log = io.StringIO()
        original = events.logger.handlers[:], events.logger.level, events.logger.propagate
        def restore():
            events.logger.handlers[:], level, events.logger.propagate = original
            events.logger.setLevel(level)
        self.addCleanup(restore)
        events.configure("fixture", self.log)

    def guard(self, values):
        with patch.object(probes.subprocess, "run", return_value=self.route), patch.object(probes, "probe_public_ip", side_effect=values):
            return probes._guard(self.cfg, "203.0.113.50")

    def cycle(self, check, low=False):
        metric = {"mbps": 1 if low else 900, "tcp_retransmits": 0, "measured_seconds": 10}
        with patch.object(probes, "resolve", side_effect=lambda host, timeout: "203.0.113.51" if host == DEFAULT["server_groups"]["primary"]["servers"][0]["host"] else "203.0.113.52"), patch.object(probes, "guard", side_effect=check if isinstance(check, Exception) else None, return_value=check), patch.object(probes, "measure", return_value=metric) as measure:
            result = Runner(self.cfg, self.store).run()
        return result, measure

    def test_one_allowed_wan_two_vpn_sources_continue_without_false_verification(self):
        data = self.guard(["192.0.2.10", "198.51.100.1", "198.51.100.1"])
        self.assertEqual(data["verification_status"], "mixed")
        self.assertFalse(data["verified"])
        self.assertFalse(data["egress_verified"])
        self.assertTrue(data["allow_measurement"])
        result, measure = self.cycle(data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(measure.call_count, 2)
        self.assertEqual(self.store.latest(), result)
        self.assertEqual(result["warnings"], ["mixed_routing"])
        self.assertEqual(len(result["attempts"][0]["route_checks"][0]["sources"]), 3)
        records = [json.loads(line) for line in self.log.getvalue().splitlines()]
        warnings = [r for r in records if r["event"] == "wan_check_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["level"], "WARNING")
        self.assertEqual(warnings[0]["test_id"], result["id"])
        self.assertEqual(warnings[0]["reason"], "mixed_routing")

    def test_matching_sources_confirm_only_https_not_router_destination_egress(self):
        data = self.guard(["192.0.2.10"] * 3)
        self.assertTrue(data["verified"])
        self.assertFalse(data["egress_verified"])
        self.assertEqual(data["gateway"], "192.168.1.1")
        self.assertNotIn("warning", data)
        result, _ = self.cycle(data)
        self.assertFalse(result.get("warnings"))
        self.assertNotIn("wan_check_warning", self.log.getvalue())

    def test_no_allowed_ip_or_lte_cannot_start_measurement(self):
        for values in (["198.51.100.1"] * 3, ["198.51.100.1", "203.0.113.1", "203.0.113.2"]):
            with self.assertRaises(probes.ProbeError) as error:
                self.guard(values)
            self.assertFalse(error.exception.details["egress_verified"])
            result, measure = self.cycle(error.exception)
            self.assertEqual(result["status"], "route_blocked")
            measure.assert_not_called()

    def test_unavailable_sources_and_insufficient_confirmations_fail_closed(self):
        for values in ([probes.ProbeError("timeout")] * 3, ["192.0.2.10", probes.ProbeError("timeout"), probes.ProbeError("dns_error")]):
            with self.assertRaisesRegex(probes.ProbeError, "route_verification_unavailable"):
                self.guard(values)

    def test_explicit_local_route_mismatch_overrides_mixed_https_evidence(self):
        self.cfg["route"]["gateway"] = "192.168.1.2"
        with patch.object(probes.subprocess, "run", return_value=self.route), patch.object(probes, "probe_public_ip") as source, self.assertRaisesRegex(probes.ProbeError, "route_gateway_mismatch"):
            probes._guard(self.cfg, "203.0.113.50")
        source.assert_not_called()

    def test_mixed_routing_never_claims_confirmed_wan_degradation(self):
        data = self.guard(["192.0.2.10", "198.51.100.1", "198.51.100.1"])
        result, _ = self.cycle(data, low=True)
        self.assertEqual(result["status"], "low_confirmed")
        self.assertFalse(result["wan_alert"])

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "tools/install.py").exists(), "Installer outside Core image")
    def test_installer_warning_is_readable_and_success_is_not_blocked(self):
        from tools import install
        data = {"status": "ok", "warnings": ["mixed_routing"], "primary": None, "confirmation": None}
        output = io.StringIO()
        with patch.object(install, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")), __import__("contextlib").redirect_stdout(output):
            self.assertEqual(install.first_test(["docker", "compose"], Path(self.tmp.name))["status"], "ok")
        self.assertIn("Возможна раздельная маршрутизация.", output.getvalue())
        self.assertIn("Проверьте маршрут к серверам измерения.", output.getvalue())
        self.assertIn("Измерение продолжено", output.getvalue())
