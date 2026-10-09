"""Regression cases from the pre-deployment audit, without public traffic."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.config import DEFAULT, load
from agent.probes import ProbeError, _guard, guard, probe_public_ip
from agent.runner import Runner
from agent.storage import Store

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/deploy.py").exists()
if HAS_TOOLS:
    from tools import deploy, install


def metric(value=900):
    return {"mbps": value, "tcp_retransmits": 0, "measured_seconds": 10, "duration_seconds": 10}


class WanAuditTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULT)
        self.cfg.update(min_interval=0, retry_delay=0)
        self.cfg["route"]["expected_public_cidrs"] = ["192.0.2.1/32"]
        self.route = subprocess.CompletedProcess([], 0, '[{"dev":"eth0"}]', '')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def guard(self, responses):
        with patch("agent.probes.subprocess.run", return_value=self.route), patch("agent.probes.probe_public_ip", side_effect=responses):
            return _guard(self.cfg, "192.0.2.100")

    def test_failed_first_witness_falls_back_and_records_error(self):
        data = self.guard([ProbeError("timeout"), "192.0.2.1", "192.0.2.1"])
        self.assertTrue(data["verified"])
        self.assertEqual(data["sources"][0]["reason"], "timeout")
        self.assertEqual(len(data["sources"]), 3)

    def test_all_sources_unavailable_and_no_heavy_test(self):
        with patch("agent.probes.subprocess.run", return_value=self.route), patch("agent.probes.probe_public_ip", side_effect=ProbeError("dns_error")), patch("agent.probes.resolve", return_value="192.0.2.100"), patch("agent.probes.guard", side_effect=lambda cfg, address, timeout: _guard(cfg, address)), patch("agent.probes.measure") as measure:
            result = Runner(self.cfg, Store(self.tmp.name)).run()
        self.assertEqual(result["status"], "route_blocked")
        check = result["attempts"][0]["route_checks"][0]
        self.assertEqual(check["verification_status"], "unavailable")
        self.assertEqual([s["reason"] for s in check["sources"]], ["dns_error"] * 3)
        measure.assert_not_called()

    def test_mismatch_and_conflict_are_distinct(self):
        for values, kind in ((["198.51.100.1"] * 3, "route_public_ip_mismatch"), (["192.0.2.1", "198.51.100.1", "192.0.2.1"], "route_verification_conflict")):
            with self.subTest(kind=kind), self.assertRaises(ProbeError) as error:
                self.guard(values)
            self.assertEqual(error.exception.kind, kind)
            self.assertFalse(error.exception.details["verified"])

    def test_minimum_confirmations_and_time_budget(self):
        self.cfg["route"]["min_confirmations"] = 2
        with self.assertRaises(ProbeError) as error:
            self.guard(["192.0.2.1", ProbeError("http_error"), ProbeError("timeout")])
        self.assertEqual(error.exception.kind, "route_verification_unavailable")
        budgets = []
        def witness(url, timeout):
            budgets.append(timeout)
            return "192.0.2.1"
        with patch("agent.probes.subprocess.run", return_value=self.route), patch("agent.probes.probe_public_ip", side_effect=witness):
            _guard(self.cfg, "192.0.2.100", timeout=1)
        self.assertTrue(all(0 < b <= 1 for b in budgets))

    def test_off_does_not_call_witnesses(self):
        self.cfg["route"]["mode"] = "off"
        with patch("agent.probes.probe_public_ip") as witness:
            self.assertFalse(_guard(self.cfg, "192.0.2.100")["verified"])
        witness.assert_not_called()

    def test_child_timeout_includes_a_stalled_resolver(self):
        real_run = subprocess.run
        def stalled(args, **kwargs):
            # Simulate a system resolver which ignores the HTTP socket timeout.
            args = [args[0], "-c", "import time; time.sleep(30)"]
            return real_run(args, **kwargs)
        start = time.monotonic()
        with patch("agent.probes.subprocess.run", side_effect=stalled), self.assertRaises(ProbeError) as error:
            probe_public_ip("https://no-network.test", 0.15)
        self.assertEqual(error.exception.kind, "timeout")
        self.assertLess(time.monotonic() - start, 3)
        with patch("agent.probes.subprocess.run", side_effect=stalled), self.assertRaises(ProbeError) as error:
            guard(self.cfg, "192.0.2.100", 0.15)
        self.assertEqual(error.exception.details["verification_status"], "unavailable")
        self.assertEqual(len(error.exception.details["sources"]), 3)

    def test_legacy_ip_url_and_secret_url_validation(self):
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps({"route": {"public_ip_url": "https://example.test"}}))
        self.assertEqual(load(path)["route"]["public_ip_urls"], ["https://example.test"])
        path.write_text(json.dumps({"route": {"public_ip_urls": ["https://secret:password@example.test"]}}))
        with self.assertRaises(ValueError) as error:
            load(path)
        self.assertNotIn("password", str(error.exception))


class PortAuditTests(unittest.TestCase):
    def cycle(self, cfg, values):
        with tempfile.TemporaryDirectory() as directory, patch("agent.probes.resolve", side_effect=lambda host, timeout: host), patch("agent.probes.guard", return_value={"verified": True}), patch("agent.probes.measure", side_effect=values):
            return Runner(cfg, Store(directory)).run()

    def test_busy_can_reach_ninth_port_without_retesting_good_port(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg.update(min_interval=0, retry_delay=0)
        result = self.cycle(cfg, [ProbeError("server_busy")] * 8 + [metric(), metric()])
        self.assertEqual(result["status"], "ok")
        self.assertEqual([a["endpoint"]["port"] for a in result["attempts"]], list(range(5201, 5210)))
        self.assertFalse(result["wan_alert"])
        self.assertTrue(all(a["switch_reason"] == "next_port_busy" for a in result["attempts"][:-1]))
        good = self.cycle(cfg, [metric(), metric()])
        self.assertEqual(len(good["attempts"]), 1)

    def test_busy_budget_falls_back_and_single_port_is_not_retried(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg.update(min_interval=0, retry_delay=0, busy_attempts_per_server=2)
        result = self.cycle(cfg, [ProbeError("server_busy")] * 3 + [metric(), metric()])
        self.assertEqual([a["server"] for a in result["attempts"]], ["hostkey-msk", "hostkey-msk", "mts-msk", "ruweb-msk"])
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["wan_alert"])

    def test_measurement_errors_keep_their_own_small_budget(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg.update(min_interval=0, retry_delay=0)
        result = self.cycle(cfg, [ProbeError("test_timeout")] * 2 + [metric(), metric()])
        self.assertEqual(len(result["attempts"]), 3)
        self.assertEqual(result["attempts"][1]["switch_reason"], "measurement_attempts_exhausted")


@unittest.skipUnless(HAS_TOOLS, "Installer sources are not in the production image")
class DeployAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("install.sh", "tools/install.py", "tools/deploy.py", "agent/config.py", "compose.yaml"):
            path = self.source / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("old")
        self.target = self.root / "stolas"

    def run_deploy(self, action=None):
        with patch.object(deploy, "run_installer", return_value=0):
            return deploy.deploy(self.source, self.target, action=action)

    def test_new_and_existing_empty_directory(self):
        self.assertEqual(self.run_deploy(), 0)
        second = self.root / "empty"
        second.mkdir()
        with patch.object(deploy, "run_installer", return_value=0):
            self.assertEqual(deploy.deploy(self.source, second), 0)
        self.assertTrue((second / "compose.yaml").is_file())

    @unittest.skipIf(os.name == "nt", "POSIX parent permissions")
    def test_empty_owned_directory_with_unwritable_parent(self):
        self.target.mkdir()
        self.root.chmod(0o555)
        try:
            self.assertEqual(self.run_deploy(), 0)
        finally:
            self.root.chmod(0o755)

    def test_foreign_files_are_untouched(self):
        self.target.mkdir()
        (self.target / "notes.txt").write_text("keep")
        with self.assertRaises(ValueError):
            self.run_deploy()
        self.assertEqual([p.name for p in self.target.iterdir()], ["notes.txt"])

    def test_reconfigure_and_update_preserve_private_files(self):
        self.run_deploy()
        (self.target / ".env").write_text("secret")
        (self.target / "config").mkdir()
        (self.target / "config/local.json").write_text("custom")
        (self.source / "agent/config.py").write_text("new")
        self.run_deploy("reconfigure")
        self.assertEqual((self.target / "agent/config.py").read_text(), "old")
        self.run_deploy("update")
        self.assertEqual((self.target / "agent/config.py").read_text(), "new")
        self.assertEqual((self.target / ".env").read_text(), "secret")
        self.assertEqual((self.target / "config/local.json").read_text(), "custom")

    def test_failed_update_restores_old_sources(self):
        self.run_deploy()
        (self.source / "agent/config.py").write_text("broken")
        with patch.object(deploy, "run_installer", side_effect=[1, 0]):
            self.assertEqual(deploy.deploy(self.source, self.target, action="update"), 1)
        self.assertEqual((self.target / "agent/config.py").read_text(), "old")
        status = json.loads((self.target / ".stolas-install-status.json").read_text())
        self.assertEqual(status["stage"], "rolled_back")

    def test_manual_rollback(self):
        self.run_deploy()
        (self.source / "agent/config.py").write_text("new")
        self.run_deploy("update")
        self.run_deploy("rollback")
        self.assertEqual((self.target / "agent/config.py").read_text(), "old")

    @unittest.skipIf(os.name == "nt", "POSIX flock/symlinks")
    def test_competing_installers_and_symlinks(self):
        with deploy.install_lock(self.target):
            with self.assertRaises(RuntimeError):
                self.run_deploy()
        link = self.root / "link"
        link.symlink_to(self.target, target_is_directory=True)
        with self.assertRaises(ValueError):
            deploy.safe_path(link)

    def test_interrupted_copy_is_recovered(self):
        self.run_deploy()
        (self.source / "agent/config.py").write_text("new")
        original = deploy.replace_file
        calls = 0
        def copy_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("interrupted")
            return original(source, target)
        with patch.object(deploy, "replace_file", side_effect=copy_once), self.assertRaises(OSError):
            self.run_deploy("update")
        self.assertEqual((self.target / "agent/config.py").read_text(), "old")

    def test_journal_recovers_incomplete_first_install_and_can_retry(self):
        self.target.mkdir()
        deploy.stage_sources(self.source, self.target, "test", "")
        # Simulate a killed process while only part of the checkout is visible.
        (self.target / "install.sh").unlink()
        self.assertEqual(self.run_deploy(), 1)
        self.assertFalse((self.target / "agent/config.py").exists())
        self.assertEqual(self.run_deploy(), 0)


@unittest.skipUnless(HAS_TOOLS, "Installer sources are not in the production image")
class TopologyAuditTests(unittest.TestCase):
    def test_topology_endpoint_rules(self):
        accepted = {"native": "http://127.0.0.1:8080", "docker": "http://172.20.0.1:8080", "lan": "https://stolas.example.test", "vpn": "http://10.8.0.1:8080", "proxy": "https://stolas.example.test"}
        for topology, endpoint in accepted.items():
            self.assertEqual(install.connection_url(endpoint, topology), endpoint)
        for topology, endpoint in (("docker", "http://127.0.0.1:8080"), ("lan", "http://192.168.1.2:8080"), ("vpn", "http://8.8.8.8:8080"), ("proxy", "http://example.test")):
            with self.assertRaises(ValueError):
                install.connection_url(endpoint, topology)

    def test_each_topology_can_be_collected_without_public_bind(self):
        for topology in ("native", "docker", "lan", "vpn", "proxy"):
            api = {"STOLAS_PORT": "8080", "STOLAS_LISTEN": "127.0.0.1"}
            answers = [topology]
            if topology == "docker": answers += ["-", "172.20.0.1", ""]
            elif topology == "vpn": answers += ["10.8.0.1", ""]
            elif topology in ("lan", "proxy"): answers += ["https://stolas.example.test"]
            else: answers += [""]
            with patch("builtins.input", side_effect=answers):
                options = install.collect_topology(api)
            self.assertEqual(options["topology"], topology)
            self.assertNotEqual(api["STOLAS_LISTEN"], "0.0.0.0")

    def test_docker_probe_sends_token_over_stdin_and_does_not_modify_container(self):
        api = {"STOLAS_API_TOKEN": "secret"}
        options = {"topology": "docker", "docker_container": "n8n", "endpoint": "http://172.20.0.1:8080"}
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            install.check_connection(ROOT, options, api, ["docker", "compose"])
        args = run.call_args.args[0]
        self.assertEqual(args[:5], ["docker", "exec", "-i", "n8n", "node"])
        self.assertNotIn("secret", " ".join(args))
        self.assertIn("secret", run.call_args.kwargs["input"])

    def test_notification_modes_configure_the_right_branches(self):
        for mode in ("alerts_only", "every_measurement", "daily_summary"):
            options = dict(endpoint="http://127.0.0.1:8080", chat_id="123", hours=3, timezone="Etc/UTC", notification_mode=mode, summary_hour=7)
            workflow = install.workflow(ROOT, options)
            nodes = {n["name"]: n for n in workflow["nodes"]}
            self.assertIn(mode, nodes["Settings"]["parameters"]["jsCode"])
            self.assertEqual(nodes["Daily summary"]["disabled"], mode != "daily_summary")
            self.assertEqual(nodes["Read summary"]["parameters"]["method"], "GET")
            self.assertFalse(workflow["active"])


class SummaryAuditTests(unittest.TestCase):
    def test_summary_uses_retained_history_and_never_invents_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            now = time.time()
            for index, (age, status, valid) in enumerate(((100, "ok", True), (200, "unavailable", False), (90000, "ok", True))):
                store.save({"id": str(index), "started_epoch": now-age, "node": "test", "status": status,
                            "primary": {"valid": valid, "download": metric(100), "upload": metric(50)} if valid else None}, 100)
            summary = store.daily_summary(now)
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["measured_count"], 1)
            self.assertEqual(summary["download_avg_mbps"], 100)
        with tempfile.TemporaryDirectory() as directory:
            empty = Store(directory).daily_summary()
            self.assertIsNone(empty["download_avg_mbps"])
            self.assertEqual(empty["count"], 0)


if __name__ == "__main__":
    unittest.main()
