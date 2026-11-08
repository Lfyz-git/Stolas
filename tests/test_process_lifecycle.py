"""Real child processes, with synthetic iperf output and no external probes."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent import probes
from agent.config import DEFAULT
from agent.probe_worker import ProbeSession
from agent.runner import Runner
from agent.storage import Store


@unittest.skipUnless(sys.platform.startswith("linux"), "Child state checks require Linux /proc")
class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.children = []
        self.threads = set(threading.enumerate())
        self.iperf_code = "import json; print(json.dumps({'end': {'sum_received': {'bits_per_second': 100000000, 'seconds': 1}, 'sum_sent': {'retransmits': 0}}}))"
        self.worker_prefix = ""
        real_popen = subprocess.Popen
        self.popen_class = real_popen

        def spawn(args, *a, **kw):
            if args[0] == "iperf3":
                args = [sys.executable, "-c", self.iperf_code]
            elif args[:2] == [sys.executable, "-c"] and "worker_main" in args[2]:
                args = [*args[:2], self.worker_prefix + args[2]]
            process = real_popen(args, *a, **kw)
            self.children.append(process)
            return process

        mocked = patch.object(subprocess, "Popen", side_effect=spawn)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def assert_reaped(self):
        self.assertTrue(self.children, "The test must actually launch children")
        for process in self.children:
            # Check before poll/wait or another Popen can accidentally reap it.
            self.assertFalse(Path(f"/proc/{process.pid}").exists(), f"Unreaped child {process.pid}")
            self.assertIsNotNone(process.returncode)
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
        deadline = time.monotonic() + 2
        while set(threading.enumerate()) - self.threads and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(set(threading.enumerate()) - self.threads, "Probe exchange threads accumulated")
        self.children.clear()

    def test_repeated_measurements_and_errors_reap_all_children(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg.update(min_interval=0, retry_delay=0, attempts_per_server=1, reserve_every=100)
        cfg["route"]["mode"] = "off"
        cfg["server_groups"]["primary"]["servers"] = [dict(id="local", host="127.0.0.1", ports=[5201], min_download_mbps=0, min_upload_mbps=0)]
        for group in ("additional", "emergency"):
            cfg["server_groups"][group]["servers"] = []
        success = self.iperf_code
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            runner = Runner(cfg, store)
            for _ in range(3):
                for code, status in ((success, "ok"), ("print('{\"error\": \"synthetic failure\"}')", "unavailable")):
                    self.iperf_code = code
                    result = runner.run()
                    self.assertEqual(result["status"], status, result)
                    self.assertEqual(store.latest(), result)
                    self.assertGreaterEqual(len(self.children), 2)  # Resolver + iperf.
                    self.assert_reaped()
                with patch.object(probes, "measure", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                    runner.run()
                self.assert_reaped()
            self.assertEqual(len(store.history()), 6)

    def test_worker_timeout_and_cancellation_leave_no_children_or_threads(self):
        self.worker_prefix = "import socket,time\noriginal=socket.gethostbyname\nsocket.gethostbyname=lambda host: time.sleep(30) if host == 'stalled.example.test' else original(host)\n"
        for _ in range(4):
            with ProbeSession() as session:
                self.assertEqual(session.resolve("127.0.0.1", 5), "127.0.0.1")
                with self.assertRaisesRegex(probes.ProbeError, "dns_error"):
                    session.resolve("stalled.example.test", .15)
            self.assert_reaped()
            with self.assertRaises(KeyboardInterrupt), ProbeSession() as session:
                self.assertEqual(session.resolve("127.0.0.1", 5), "127.0.0.1")
                with patch("agent.probe_worker.queue.Queue.get", side_effect=KeyboardInterrupt):
                    session.resolve("stalled.example.test", 5)
            self.assert_reaped()
        self.worker_prefix = "import os; os._exit(23)\n"
        for _ in range(4):
            with ProbeSession() as session, self.assertRaisesRegex(probes.ProbeError, "dns_error"):
                session.resolve("127.0.0.1", 5)
            self.assert_reaped()

    def test_iperf_timeout_and_cancellation_reap_the_real_process(self):
        self.iperf_code = "import time; time.sleep(30)"
        # Save the real class: Popen is instrumented above, not a fake process.
        for _ in range(4):
            with self.assertRaisesRegex(probes.ProbeError, "test_timeout"):
                probes.measure(DEFAULT, "127.0.0.1", 5201, False, .15)
            self.assert_reaped()
            # Inject cancellation at communicate while retaining real kill/wait.
            with patch.object(self.popen_class, "communicate", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                probes.measure(DEFAULT, "127.0.0.1", 5201, False, 5)
            self.assert_reaped()
