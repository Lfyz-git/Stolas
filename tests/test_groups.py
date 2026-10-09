"""Group routing, server selection and configuration compatibility."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.config import DEFAULT, GROUPS, load
from agent.probes import ProbeError
from agent.runner import Runner
from agent.storage import Store


def server(name, address):
    return dict(id=name, host=address, ports=[5201], min_download_mbps=500, min_upload_mbps=500)


def metric(speed=900):
    return dict(mbps=speed, tcp_retransmits=0, measured_seconds=10,
                duration_seconds=10, latency_ms=None, latency_method=None)


class GroupConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def load_config(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return load(self.path)

    def test_no_server_count_limit_in_any_group(self):
        groups = {name: {"selection": "random", "servers": [server(f"{name}-{index}", f"{name}-{index}.example.test") for index in range(100)]} for name in GROUPS}
        cfg = self.load_config({"server_groups": groups})
        self.assertEqual(cfg["server_groups"], groups)

    def test_primary_is_required_and_other_groups_may_be_empty(self):
        groups = copy.deepcopy(DEFAULT["server_groups"])
        groups["additional"]["servers"] = []
        groups["emergency"]["servers"] = []
        self.assertEqual(self.load_config({"server_groups": groups})["server_groups"], groups)
        groups["primary"]["servers"] = []
        with self.assertRaises(ValueError):
            self.load_config({"server_groups": groups})

    def test_invalid_groups_selection_and_cross_group_duplicates(self):
        cases = []
        groups = copy.deepcopy(DEFAULT["server_groups"])
        groups.pop("emergency")
        cases.append(groups)
        groups = copy.deepcopy(DEFAULT["server_groups"])
        groups["additional"]["selection"] = "first"
        cases.append(groups)
        groups = copy.deepcopy(DEFAULT["server_groups"])
        groups["emergency"]["servers"] = groups["primary"]["servers"]
        cases.append(groups)
        groups = copy.deepcopy(DEFAULT["server_groups"])
        groups["primary"]["servers"][0]["min_download_mbps"] = float("nan")
        cases.append(groups)
        for groups in cases:
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                self.load_config({"server_groups": groups})

    def test_legacy_list_migrates_without_losing_custom_servers(self):
        entries = [server(f"old-{i}", f"old-{i}.example.test") for i in range(25)]
        cfg = self.load_config({"servers": entries})
        self.assertNotIn("servers", cfg)
        self.assertEqual(cfg["server_groups"]["primary"]["servers"], entries[:1])
        self.assertEqual(cfg["server_groups"]["additional"]["servers"], entries[1:2])
        self.assertEqual(cfg["server_groups"]["emergency"]["servers"], entries[2:])
        for count in (1, 2):
            cfg = self.load_config({"servers": entries[:count]})
            self.assertEqual(sum(len(g["servers"]) for g in cfg["server_groups"].values()), count)
        with self.assertRaises(ValueError):
            self.load_config(dict(servers=entries, server_groups=DEFAULT["server_groups"]))

    def test_group_environment_override_and_conflicting_legacy_env(self):
        groups = copy.deepcopy(DEFAULT["server_groups"])
        groups["primary"]["selection"] = "random"
        with patch.dict("os.environ", {"STOLAS_SERVER_GROUPS": json.dumps(groups)}):
            self.assertEqual(load()["server_groups"], groups)
            with patch.dict("os.environ", {"STOLAS_SERVERS": "[]"}), self.assertRaises(ValueError):
                load()


class GroupRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULT)
        self.cfg.update(min_interval=0, retry_delay=0, attempts_per_server=1)
        self.cfg["server_groups"] = {
            name: {"selection": "sequential", "servers": [server(f"{name}-{i}", f"192.0.{subnet}.{i + 1}") for i in range(3)]}
            for name, subnet in zip(GROUPS, (2, 3, 4))
        }

    def cycle(self, values=None, resolve=None):
        # Reopen storage each time to exercise persistence across API/CLI restarts.
        with patch("agent.probes.resolve", side_effect=resolve or (lambda host, timeout: host)), patch("agent.probes.guard", return_value={"verified": True}), patch("agent.probes.measure", side_effect=values, return_value=metric()):
            return Runner(self.cfg, Store(self.tmp.name)).run()

    def test_healthy_primary_does_not_visit_other_groups(self):
        result = self.cycle()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([s["group"] for s in result["attempts"]], ["primary"])
        self.assertEqual(result["primary"]["selection"], "sequential")

    def test_sequential_rotation_survives_restart_and_history_pruning(self):
        self.cfg["history_limit"] = 1
        self.assertEqual([self.cycle()["primary"]["server"] for _ in range(4)], ["primary-0", "primary-1", "primary-2", "primary-0"])
        self.assertEqual(len(Store(self.tmp.name).history()), 1)

    def test_next_cycle_continues_after_last_attempted_primary(self):
        def resolve(host, timeout):
            if host == "192.0.2.1":
                raise ProbeError("dns_failed")
            return host
        first = self.cycle(resolve=resolve)
        self.assertEqual(first["primary"]["server"], "primary-1")
        self.assertEqual(first["errors"][0]["group"], "primary")
        self.assertEqual(self.cycle()["primary"]["server"], "primary-2")

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

    def test_low_speed_uses_additional_for_confirmation(self):
        result = self.cycle([metric(10), metric(), metric(20), metric()])
        self.assertEqual(result["status"], "low_confirmed")
        self.assertEqual(result["confirmation"]["group"], "additional")
        self.assertEqual([s["reason"] for s in result["attempts"]], ["primary", "confirmation"])

    def test_alias_is_skipped_and_next_additional_can_confirm(self):
        def resolve(host, timeout):
            return "192.0.2.1" if host == "192.0.3.1" else host
        result = self.cycle([metric(10), metric(), metric(20), metric()], resolve=resolve)
        self.assertEqual(result["confirmation"]["server"], "additional-1")
        self.assertEqual(result["errors"][0]["reason"], "confirmation_not_independent")
        self.assertEqual(len(result["attempts"]), 2)

    def test_emergency_confirms_when_additional_is_unavailable(self):
        result = self.cycle([metric(10), metric()] + [ProbeError("server_busy")] * 3 + [metric(20), metric()])
        self.assertEqual(result["confirmation"]["group"], "emergency")
        self.assertTrue(result["wan_alert"])

    def test_disagreement_does_not_escalate_to_emergency(self):
        result = self.cycle([metric(10), metric(), metric(), metric()])
        self.assertEqual(result["status"], "server_disagreement")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertFalse(result["wan_alert"])

    def test_empty_secondary_groups_leave_low_speed_unconfirmed(self):
        for name in ("additional", "emergency"):
            self.cfg["server_groups"][name]["servers"] = []
        result = self.cycle([metric(10), metric()])
        self.assertEqual(result["status"], "low_unconfirmed")
        self.assertFalse(result["wan_alert"])

    def test_audit_selects_one_server_per_group_and_advances_only_when_used(self):
        self.cfg.update(reserve_every=2, history_limit=1)
        cycles = [self.cycle() for _ in range(4)]
        self.assertEqual([len(c["attempts"]) for c in cycles], [1, 3, 1, 3])
        self.assertEqual([c["attempts"][1]["server"] for c in (cycles[1], cycles[3])], ["additional-0", "additional-1"])
        self.assertEqual([c["attempts"][2]["server"] for c in (cycles[1], cycles[3])], ["emergency-0", "emergency-1"])

    def test_deadline_stops_a_large_group_after_dns_failures(self):
        self.cfg["server_groups"]["primary"]["servers"] = [server(f"p{i}", f"p{i}.example.test") for i in range(100)]
        checks = 0

        def remaining(deadline):
            nonlocal checks
            checks += 1
            if checks > 6:
                raise ProbeError("cycle_timeout")
            return 1

        with patch.object(Runner, "_remaining", side_effect=remaining):
            result = self.cycle(resolve=lambda host, timeout: (_ for _ in ()).throw(ProbeError("dns_failed")))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["errors"][-1]["reason"], "cycle_timeout")
        self.assertLess(len(result["errors"]), 10)


if __name__ == "__main__":
    unittest.main()
