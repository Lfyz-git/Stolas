import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from agent.api import make_server
from agent.config import DEFAULT, load
from agent.probes import ProbeError, parse_iperf
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

    def test_partial_pair_never_mixes_servers(self):
        r = self.cycle([metric(5), ProbeError("test_timeout"), ProbeError("test_timeout"), metric(900), metric(800)])
        self.assertEqual(r["primary"]["server"], "mts-msk")
        self.assertEqual(r["attempts"][0]["download"]["mbps"], 5)
        self.assertFalse(r["attempts"][0]["valid"])

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

    def test_parser_receiver_rate_sender_retransmits(self):
        sample = {"end": {"sum_received": {"bits_per_second": 925000000, "seconds": 10}, "sum_sent": {"retransmits": 7}}}
        self.assertEqual(parse_iperf(sample)["mbps"], 925)
        self.assertEqual(parse_iperf(sample)["tcp_retransmits"], 7)
        del sample["end"]["sum_sent"]["retransmits"]
        self.assertIsNone(parse_iperf(sample)["tcp_retransmits"])
        sample["end"]["sum_received"]["bits_per_second"] = float("nan")
        with self.assertRaises(ProbeError):
            parse_iperf(sample)

    def test_cli_real_process_fail_closed_json(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg["server_groups"]["primary"]["servers"] = [dict(cfg["server_groups"]["primary"]["servers"][0], host="127.0.0.1")]
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w") as file:
            json.dump(cfg, file)
        p = subprocess.run([sys.executable, "-m", "agent", "run", "--config", path, "--data", self.tmp.name], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 2, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result["status"], "route_blocked")
        self.assertIn("route_unconfigured", result["attempts"][0]["errors"])

    def test_config_rejects_options_and_invalid_limits(self):
        with patch.dict(os.environ, {"STOLAS_SECONDS": "0"}):
            with self.assertRaises(ValueError):
                load()
        groups = copy.deepcopy(DEFAULT["server_groups"])
        groups["primary"]["servers"][0]["host"] = "--help"
        with patch.dict(os.environ, {"STOLAS_SERVER_GROUPS": json.dumps(groups)}):
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

    def test_daily_summary_is_authenticated_and_does_not_measure(self):
        self.assertEqual(self.request("/v1/summary/daily", auth=False)[0], 401)
        with patch.object(self.runner, "run") as heavy:
            code, body = self.request("/v1/summary/daily")
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 0)
        self.assertIsNone(body["download_avg_mbps"])
        heavy.assert_not_called()

    def test_reject_empty_token(self):
        with self.assertRaises(ValueError):
            make_server(self.runner, ("127.0.0.1", 0), "")


class SummaryTests(unittest.TestCase):
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
