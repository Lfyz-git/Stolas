import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from agent.api import make_server
from agent.config import DEFAULT, load
from agent.probes import ProbeError, _guard, parse_iperf, measure
from agent.runner import Cooldown, Runner
from agent.storage import Busy, Store


def metric(speed):
    return {"mbps": speed, "tcp_retransmits": 2, "measured_seconds": 10, "duration_seconds": 10, "latency_ms": None, "latency_method": None}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULT)
        self.cfg.update(retry_delay=0, min_interval=0)
        self.store = Store(self.tmp.name)
        self.runner = Runner(self.cfg, self.store)

    def cycle(self, values, guard=None):
        with patch("agent.probes.resolve", side_effect=lambda host, timeout: {"spd-rudp.hostkey.ru": "192.0.2.1", "mskst.st.mtsws.net": "192.0.2.2", "msk.speed.ruweb.net": "192.0.2.3"}[host]), patch("agent.probes.guard", side_effect=guard, return_value={"verified": True}), patch("agent.probes.measure", side_effect=values):
            return self.runner.run()

    def test_busy_rotates_port_without_wan_alert(self):
        r = self.cycle([ProbeError("server_busy"), metric(900), metric(900)])
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["wan_alert"])
        self.assertEqual([s["endpoint"]["port"] for s in r["attempts"]], [5201, 5202])

    def test_all_busy_is_unavailable(self):
        r = self.cycle([ProbeError("server_busy")] * 6)
        self.assertEqual(r["status"], "unavailable")
        self.assertFalse(r["wan_alert"])
        self.assertEqual(len(r["attempts"]), 6)

    def test_low_confirmed_and_original_preserved(self):
        r = self.cycle([metric(10), metric(900), metric(20), metric(800)])
        self.assertEqual(r["status"], "low_confirmed")
        self.assertTrue(r["wan_alert"])
        self.assertEqual(r["primary"]["download"]["mbps"], 10)
        self.assertEqual(r["confirmation"]["download"]["mbps"], 20)
        self.assertEqual(self.store.latest(), r)

    def test_different_low_directions_do_not_confirm(self):
        r = self.cycle([metric(10), metric(900), metric(900), metric(10)])
        self.assertEqual(r["status"], "server_disagreement")
        self.assertFalse(r["wan_alert"])

    def test_disabled_guard_suppresses_wan_alert(self):
        self.cfg["route"]["mode"] = "off"
        r = self.cycle([metric(1)] * 4)
        self.assertFalse(r["wan_alert"])

    def test_partial_pair_never_mixes_servers(self):
        r = self.cycle([metric(5), ProbeError("test_timeout"), ProbeError("server_busy"), metric(900), metric(800)])
        self.assertEqual(r["primary"]["server"], "mts-msk")
        self.assertEqual(r["attempts"][0]["download"]["mbps"], 5)
        self.assertFalse(r["attempts"][0]["valid"])

    def test_route_change_retains_raw_measurement(self):
        r = self.cycle([metric(900)], [dict(verified=True), ProbeError("route_public_ip_mismatch")])
        self.assertEqual(r["status"], "route_blocked")
        self.assertEqual(r["attempts"][0]["download"]["mbps"], 900)
        self.assertFalse(r["attempts"][0]["valid"])
        self.assertFalse(r["wan_alert"])

    def test_reserve_audit(self):
        self.cfg["reserve_every"] = 1
        r = self.cycle([metric(900)] * 6)
        self.assertEqual(len(r["attempts"]), 3)
        self.assertEqual(r["attempts"][2]["reason"], "reserve")

    def test_persistent_cooldown(self):
        self.cycle([metric(900)] * 2)
        self.cfg["min_interval"] = 300
        with self.assertRaises(Cooldown):
            Runner(self.cfg, Store(self.tmp.name)).run()

    def test_lock(self):
        with self.store.lock():
            with self.assertRaises(Busy):
                with Store(self.tmp.name).lock():
                    pass

    def test_retention_and_sequence(self):
        self.cfg["history_limit"] = 1
        self.cycle([metric(900)] * 2)
        self.cycle([metric(900)] * 2)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(self.store.sequence(), 2)

    def test_parser_receiver_rate_sender_retransmits(self):
        sample = {"end": {"sum_received": {"bits_per_second": 925000000, "seconds": 10}, "sum_sent": {"retransmits": 7}}}
        self.assertEqual(parse_iperf(sample)["mbps"], 925)
        self.assertEqual(parse_iperf(sample)["tcp_retransmits"], 7)
        del sample["end"]["sum_sent"]["retransmits"]
        self.assertIsNone(parse_iperf(sample)["tcp_retransmits"])
        sample["end"]["sum_received"]["bits_per_second"] = float("nan")
        with self.assertRaises(ProbeError):
            parse_iperf(sample)

    def test_subprocess_command_and_timeout(self):
        with patch("agent.probes.subprocess.run", side_effect=subprocess.TimeoutExpired("iperf3", 1)) as run:
            with self.assertRaisesRegex(ProbeError, "test_timeout"):
                measure(self.cfg, "192.0.2.1", 5201, True, 1)
            args = run.call_args.args[0]
            self.assertIn("-R", args)
            self.assertIn("-4", args)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_unconfigured_route_fails_closed(self):
        with self.assertRaisesRegex(ProbeError, "route_unconfigured"):
            _guard(self.cfg, "192.0.2.1")

    def test_route_interface_mismatch(self):
        self.cfg["route"].update(expected_public_cidrs=["192.0.2.0/24"], interface="wan0")
        response = subprocess.CompletedProcess([], 0, '[{"dev":"vpn0"}]', '')
        with patch("agent.probes.subprocess.run", return_value=response), patch("agent.probes.urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(ProbeError, "route_interface_mismatch"):
                _guard(self.cfg, "192.0.2.1")
            opener.assert_not_called()

    def test_route_egress_mismatch(self):
        self.cfg["route"]["expected_public_cidrs"] = ["192.0.2.0/24"]
        response = subprocess.CompletedProcess([], 0, '[{"dev":"eth0"}]', '')
        with patch("agent.probes.subprocess.run", return_value=response), patch("agent.probes.urllib.request.build_opener") as opener:
            reply = opener.return_value.open.return_value.__enter__.return_value
            reply.geturl.return_value = "https://example.invalid"
            reply.read.return_value = b"198.51.100.1"
            with self.assertRaisesRegex(ProbeError, "route_public_ip_mismatch"):
                _guard(self.cfg, "192.0.2.1")
            reply.read.return_value = b"192.0.2.10"
            self.assertTrue(_guard(self.cfg, "192.0.2.1")["verified"])

    def test_cli_real_process_fail_closed_json(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg["servers"] = [dict(cfg["servers"][0], host="127.0.0.1")]
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w") as file:
            json.dump(cfg, file)
        p = subprocess.run([sys.executable, "-m", "agent", "run", "--config", path, "--data", self.tmp.name], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 2, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result["status"], "route_blocked")
        self.assertIn("route_unconfigured", result["attempts"][0]["errors"])

    def test_alias_confirmation_continues_to_third_server(self):
        with patch("agent.probes.resolve", side_effect=["192.0.2.1", "192.0.2.1", "192.0.2.3"]), patch("agent.probes.guard", return_value={"verified": True}), patch("agent.probes.measure", side_effect=[metric(1)] * 6):
            r = self.runner.run()
        self.assertEqual(r["confirmation"]["server"], "ruweb-msk")

    def test_cycle_deadline_is_recorded(self):
        with patch.object(Runner, "_remaining", side_effect=ProbeError("cycle_timeout")):
            r = self.runner.run()
        self.assertEqual(r["status"], "unavailable")
        self.assertFalse(r["wan_alert"])
        self.assertTrue(any(e["reason"] == "cycle_timeout" for e in r["errors"]))

    def test_config_rejects_options_and_invalid_limits(self):
        with patch.dict(os.environ, {"STOLAS_SECONDS": "0"}):
            with self.assertRaises(ValueError):
                load()
        with patch.dict(os.environ, {"STOLAS_SERVERS": json.dumps([dict(DEFAULT["servers"][0], host="--help")])}):
            with self.assertRaises(ValueError):
                load()


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = "x" * 40
        self.runner = Runner(copy.deepcopy(DEFAULT), Store(self.tmp.name))
        self.server = make_server(self.runner, ("127.0.0.1", 0), self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, method="GET", auth=True, body=None):
        headers = {"Authorization": "Bearer " + self.token} if auth else {}
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_api_auth_and_validation(self):
        self.assertEqual(self.request("/healthz", auth=False)[0], 401)
        self.assertEqual(self.request("/v1/tests", "POST", auth=False)[0], 401)
        self.assertEqual(self.request("/healthz")[0], 200)
        self.assertEqual(self.request("/v1/results/latest")[0], 404)
        self.assertEqual(self.request("/v1/tests", "POST", body=b'{"host":"evil"}')[0], 400)
        with patch.object(self.runner, "run", return_value={"status": "ok"}):
            self.assertEqual(self.request("/v1/tests", "POST"), (200, {"status": "ok"}))

    def test_busy_and_cooldown(self):
        for error, code in [(Busy(), 409), (Cooldown(), 429)]:
            with patch.object(self.runner, "run", side_effect=error):
                self.assertEqual(self.request("/v1/tests", "POST")[0], code)

    def test_reject_empty_token(self):
        with self.assertRaises(ValueError):
            make_server(self.runner, ("127.0.0.1", 0), "")


if __name__ == "__main__":
    unittest.main()
