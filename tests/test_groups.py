"""Group routing, server selection and bounded retry budgets."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from agent.config import DEFAULT, GROUPS
from agent.probes import ProbeError
from agent.runner import Runner
from agent.storage import Store


def server(name, address):
    return dict(id=name, host=address, ports=[5201], min_download_mbps=500, min_upload_mbps=500)


def metric(speed=900):
    return dict(mbps=speed, tcp_retransmits=0, measured_seconds=10,
                duration_seconds=10, latency_ms=None, latency_method=None)


class GroupRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULT)
        self.cfg.update(min_interval=0, retry_delay=0, attempts_per_server=1)
        self.cfg["server_groups"] = {
            name: {"selection": "sequential", "servers": [server(f"{name}-{i}", f"{subnet}.{i + 1}") for i in range(3)]}
            for name, subnet in zip(GROUPS, ("192.0.2", "198.51.100", "203.0.113"))
        }

    def cycle(self, values=None, resolve=None):
        # Reopen storage each time to exercise persistence across API/CLI restarts.
        with patch("agent.probes.resolve", side_effect=resolve or (lambda host, timeout: host)), patch("agent.probes.guard", return_value={"verified": True}), patch("agent.probes.measure", side_effect=values, return_value=metric()):
            return Runner(self.cfg, Store(self.tmp.name)).run()

    def test_sequential_rotation_survives_restart_and_history_pruning(self):
        self.cfg["history_limit"] = 1
        self.assertEqual([self.cycle()["primary"]["server"] for _ in range(4)], ["primary-0", "primary-1", "primary-2", "primary-0"])
        self.assertEqual(len(Store(self.tmp.name).history()), 1)

    def test_random_tries_each_server_once_then_moves_to_additional(self):
        self.cfg["server_groups"]["primary"]["selection"] = "random"
        with patch("agent.runner.random.shuffle", side_effect=lambda values: values.reverse()) as shuffle:
            result = self.cycle([ProbeError("server_busy")] * 3 + [metric(), metric()])
        self.assertEqual([s["server"] for s in result["attempts"]], ["primary-2", "primary-1", "primary-0", "additional-0"])
        self.assertEqual(result["primary"]["group"], "additional")
        shuffle.assert_called_once()
        # Random selection does not advance the sequential cursor.
        self.cfg["server_groups"]["primary"]["selection"] = "sequential"
        self.assertEqual(self.cycle()["primary"]["server"], "primary-0")

    def test_emergency_is_used_only_after_exhausting_both_earlier_groups(self):
        result = self.cycle([ProbeError("server_busy")] * 6 + [metric(), metric()])
        self.assertEqual([s["group"] for s in result["attempts"]], ["primary"] * 3 + ["additional"] * 3 + ["emergency"])
        self.assertEqual(result["primary"]["group"], "emergency")
        self.assertEqual(result["status"], "ok")

    def test_alias_is_skipped_and_next_additional_can_confirm(self):
        def resolve(host, timeout):
            return "192.0.2.1" if host == "198.51.100.1" else host
        result = self.cycle([metric(10), metric(), metric(20), metric()], resolve=resolve)
        self.assertEqual(result["confirmation"]["server"], "additional-1")
        self.assertEqual(result["errors"][0]["reason"], "confirmation_not_independent")
        self.assertEqual(len(result["attempts"]), 2)


class RetryBudgetTests(unittest.TestCase):
    def cycle(self, cfg, values):
        with tempfile.TemporaryDirectory() as directory, patch("agent.probes.resolve", side_effect=lambda host, timeout: host), patch("agent.probes.guard", return_value={"verified": True}), patch("agent.probes.measure", side_effect=values):
            return Runner(cfg, Store(directory)).run()

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


if __name__ == "__main__":
    unittest.main()
