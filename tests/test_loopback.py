"""Real iperf3 loopback integration; never contacts public speed servers."""
import copy
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from agent.config import DEFAULT
from agent.runner import Runner
from agent.storage import Store


@unittest.skipUnless(shutil.which("iperf3"), "iperf3 unavailable; run Docker integration")
class LoopbackTests(unittest.TestCase):
    def test_real_upload_download(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen(["iperf3", "-s", "-B", "127.0.0.1", "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Check readiness without using an iperf control session.
            time.sleep(0.5)
            self.assertIsNone(server.poll())
            cfg = copy.deepcopy(DEFAULT)
            cfg.update(seconds=1, parallel=1, attempts_per_server=1, min_interval=0)
            cfg["route"]["mode"] = "off"
            cfg["server_groups"]["primary"]["servers"] = [{"id": "loopback", "host": "127.0.0.1", "ports": [port], "min_download_mbps": 0, "min_upload_mbps": 0}]
            for group in ("additional", "emergency"):
                cfg["server_groups"][group]["servers"] = []
            with tempfile.TemporaryDirectory() as directory:
                result = Runner(cfg, Store(directory)).run()
                self.assertEqual(result["status"], "ok", result)
                for direction in ("download", "upload"):
                    self.assertGreater(result["primary"][direction]["mbps"], 0)
                    self.assertIsNotNone(result["primary"][direction]["tcp_retransmits"])
        finally:
            server.terminate()
            server.wait(timeout=5)
