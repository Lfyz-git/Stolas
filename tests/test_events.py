import copy
import io
import json
import os
import tempfile
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from agent import events
from agent.api import make_server
from agent.config import DEFAULT
from agent.probes import ProbeError
from agent.runner import Runner
from agent.storage import Store


def metric(speed=900):
    return {"mbps": speed}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        old = events.logger.handlers[:], events.logger.level, events.logger.propagate
        self.addCleanup(self.restore, old)
        with patch.dict(os.environ, {"STOLAS_LOG_LEVEL": "INFO", "STOLAS_LOG_FORMAT": "json"}):
            events.configure("test-node", self.stream)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(DEFAULT)
        self.cfg.update(min_interval=0, retry_delay=0)
        self.runner = Runner(self.cfg, Store(self.tmp.name))

    def restore(self, old):
        events.logger.handlers, events.logger.level, events.logger.propagate = old

    def records(self):
        records = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        for row in records:
            self.assertTrue({"timestamp", "level", "service", "node", "event"} <= row.keys())
            self.assertEqual(row["service"], "stolas")
            self.assertEqual(row["node"], "test-node")
            self.assertIn(row["event"], events.EVENTS)
        return records

    def cycle(self, measures, guard=None):
        hosts = {s["host"]: str(i + 1) + ".0.0.1" for i, s in enumerate(g["servers"][0] for g in self.cfg["server_groups"].values())}
        with patch("agent.probes.resolve", side_effect=lambda host, timeout: hosts[host]), patch("agent.probes.measure", side_effect=measures), patch("agent.probes.guard", side_effect=guard, return_value={"verified": True}):
            return self.runner.run()

    def test_normal_measurement_is_one_info_event_and_history_is_independent(self):
        result = self.cycle([metric(), metric()])
        rows = self.records()
        self.assertEqual([r["event"] for r in rows], ["measurement_completed"])
        self.assertEqual(rows[0]["test_id"], result["id"])
        self.assertEqual(rows[0]["download_mbps"], 900)
        self.assertEqual(rows[0]["level"], "INFO")
        self.assertEqual(self.runner.store.latest(), result)

    def test_debug_start_retry_and_busy_share_measurement_id(self):
        events.logger.setLevel("DEBUG")
        result = self.cycle([ProbeError("server_busy"), metric(), metric()])
        rows = self.records()
        self.assertEqual([r["event"] for r in rows], ["measurement_started", "server_busy", "measurement_retry", "measurement_completed"])
        self.assertEqual({r["test_id"] for r in rows}, {result["id"]})
        self.assertEqual(rows[0]["level"], "DEBUG")
        self.assertEqual(rows[1]["level"], "WARNING")

    def test_wan_failure_and_confirmed_degradation_have_stable_levels(self):
        result = self.cycle([], ProbeError("route_public_ip_mismatch"))
        self.assertEqual(result["status"], "route_blocked")
        self.assertEqual(self.records()[0]["event"], "wan_check_failed")
        self.assertEqual(self.records()[0]["level"], "ERROR")
        self.stream.seek(0); self.stream.truncate(0)
        result = self.cycle([metric(10)] * 4)
        rows = self.records()
        self.assertEqual(rows[-1]["event"], "speed_degradation_confirmed")
        self.assertEqual(rows[-1]["level"], "WARNING")
        self.assertEqual(rows[-1]["test_id"], result["id"])

    def test_storage_failure_does_not_log_exception_or_its_secret(self):
        with patch.object(self.runner.store, "save", side_effect=OSError("password=my-secret")), self.assertRaises(OSError):
            self.cycle([metric(), metric()])
        self.assertEqual(self.records()[-1]["event"], "history_error")
        self.assertNotIn("my-secret", self.stream.getvalue())

    def test_closed_collector_does_not_break_measurements(self):
        self.stream.close()
        self.assertEqual(self.cycle([metric(), metric()])["status"], "ok")

    def test_json_escaping_and_unknown_fields_rejected(self):
        events.emit("server_error", "WARNING", server='quoted"name\nnext', reason="iperf_error")
        self.assertEqual(len(self.stream.getvalue().splitlines()), 1)
        self.assertEqual(self.records()[0]["server"], 'quoted"name\nnext')
        with self.assertRaises(ValueError):
            events.emit("server_error", token="secret")
        with self.assertRaises(ValueError):
            events.emit("new_unstable_event")

    def test_http_error_never_contains_headers_url_or_exception_secrets(self):
        token = "private-bearer-" * 4
        server = make_server(self.runner, ("127.0.0.1", 0), token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)
        for path, auth in (("/private?password=url-secret", None), ("/v1/tests", token)):
            request = urllib.request.Request(base + path, method="POST", headers={"Authorization": "Bearer " + (auth or "header-secret")})
            with patch.object(self.runner, "run", side_effect=RuntimeError("Telegram bot secret private-exception")), self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(request)
        rows = self.records()
        self.assertEqual([r["http_status"] for r in rows], [401, 500])
        for secret in (token, "header-secret", "url-secret", "private-exception", "Telegram"):
            self.assertNotIn(secret, self.stream.getvalue())

    def test_text_and_invalid_logging_configuration(self):
        with patch.dict(os.environ, {"STOLAS_LOG_LEVEL": "ERROR", "STOLAS_LOG_FORMAT": "text"}):
            events.configure("test-node", self.stream)
        events.emit("measurement_completed", status="ok")
        events.emit("history_error", "ERROR", operation="save")
        self.assertIn("ERROR history_error", self.stream.getvalue())
        self.assertNotIn("measurement_completed", self.stream.getvalue())
        with patch.dict(os.environ, {"STOLAS_LOG_LEVEL": "TRACE"}), self.assertRaises(ValueError):
            events.configure("test-node", self.stream)

    def test_cli_result_and_diagnostic_streams_are_separate_and_secret_safe(self):
        config = os.path.join(self.tmp.name, "config.json")
        with open(config, "w") as file:
            json.dump(self.cfg, file)
        env = {k: v for k, v in os.environ.items() if not k.startswith("STOLAS_")}
        env.update(STOLAS_LOG_LEVEL="DEBUG", STOLAS_LOG_FORMAT="json", STOLAS_API_TOKEN="private-cli-token")
        result = subprocess.run([sys.executable, "-m", "agent", "validate", "--config", config], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["valid"])
        rows = [json.loads(line) for line in result.stderr.splitlines()]
        self.assertEqual(rows[0]["event"], "configuration_loaded")
        with open(config, "w") as file:
            file.write('{"password":"exception-secret",')
        result = subprocess.run([sys.executable, "-m", "agent", "validate", "--config", config], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr.splitlines()[-1])["event"], "application_failed")
        self.assertNotIn("exception-secret", result.stderr)
        self.assertNotIn("private-cli-token", result.stderr)
