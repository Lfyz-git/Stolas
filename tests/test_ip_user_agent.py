"""Local TLS witnesses exercise bounded workers and the isolated guard process.

The localhost PEM fixtures are public test-only credentials, never server secrets.
"""
import copy
import io
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from agent import __version__, probes
from agent.config import DEFAULT
from agent.runner import Runner
from agent.storage import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
USER_AGENT = "Stolas/" + __version__


class UserAgentTests(unittest.TestCase):
    def test_request_carries_version_without_relaxing_transport(self):
        for body in (b'192.0.2.1', b'"192.0.2.1"'):
            with patch.object(probes.urllib.request, "build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value = io.BytesIO(body)
                self.assertEqual(probes._read_public_ip("https://example.test/ip", 3), "192.0.2.1")
            handlers = build.call_args.args
            self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], probes.NoRedirect)
            request = build.return_value.open.call_args.args[0]
            self.assertIsInstance(request, urllib.request.Request)
            self.assertEqual(request.full_url, "https://example.test/ip")
            self.assertEqual(request.get_header("User-agent"), USER_AGENT)
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 3)

    @unittest.skipUnless((FIXTURES.parents[1] / "tools/layout.py").exists(), "Host release metadata outside Core image")
    def test_agent_version_matches_installer_release(self):
        from tools.layout import VERSION
        self.assertEqual(__version__, VERSION)


class LocalHttpsProbeTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("User-Agent")))
                # Reproduce a witness that stalls for urllib's default identity.
                if self.headers.get("User-Agent") != USER_AGENT or self.path == "/slow":
                    time.sleep(3.5)
                if self.path == "/http-error":
                    self.send_response(503)
                    body = b"unavailable"
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/plain")
                    body = b""
                else:
                    self.send_response(200)
                    body = {"/plain": b"192.0.2.1\n", "/json": b'"192.0.2.1"',
                            "/invalid": b'{"ip":"192.0.2.1"}', "/vpn": b'"198.51.100.1"'}.get(self.path, b'"192.0.2.1"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (OSError, ssl.SSLError):
                    pass  # Expected when a bounded probe times out.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(FIXTURES / "localhost-test-cert.pem", FIXTURES / "localhost-test-key.pem")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(lambda: thread.join(timeout=3))
        self.addCleanup(self.server.shutdown)
        environment = patch.dict(os.environ, {"SSL_CERT_FILE": str(FIXTURES / "localhost-test-cert.pem"),
                                               "HTTPS_PROXY": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.base = "https://127.0.0.1:" + str(self.server.server_port)

    def test_real_three_second_probe_repeated_plain_and_json(self):
        for path in ("/plain", "/json", "/json"):
            self.assertEqual(probes.probe_public_ip(self.base + path, 3), "192.0.2.1")
        self.assertEqual(self.requests, [(path, USER_AGENT) for path in ("/plain", "/json", "/json")])

    def test_probe_avoids_interpreter_startup(self):
        # An interpreter/import delay longer than source_timeout cannot affect a
        # witness: there must be no subprocess to start in this call at all.
        with patch.object(probes.subprocess, "run", side_effect=AssertionError("unexpected interpreter")) as run:
            self.assertEqual(probes.probe_public_ip(self.base + "/json", 3), "192.0.2.1")
        run.assert_not_called()

    def child_environment(self, startup_delay=0, stalled_dns=False):
        import sys
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        starts = directory / "starts"
        script = "import time, pathlib, socket\n"
        script += "with pathlib.Path(" + repr(str(starts)) + ").open('a') as log: log.write('started\\n')\n"
        script += "time.sleep(" + repr(startup_delay) + ")\n"
        if stalled_dns:
            script += "real_resolve = socket.getaddrinfo\ndef resolve(host, *args, **kwargs):\n"
            script += "    if host == 'stalled.example.test': time.sleep(30)\n"
            script += "    return real_resolve(host, *args, **kwargs)\nsocket.getaddrinfo = resolve\n"
        (directory / "sitecustomize.py").write_text(script)
        environment = patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join([str(directory), *sys.path])})
        environment.start()
        self.addCleanup(environment.stop)
        return starts

    def test_real_probe_errors_keep_status_and_redirects_are_blocked(self):
        for path, reason in (("/http-error", "http_error"), ("/invalid", "invalid_ipv4"), ("/redirect", "http_error")):
            with self.subTest(path=path), self.assertRaisesRegex(probes.ProbeError, reason):
                probes.probe_public_ip(self.base + path, 3)
        self.assertNotIn(("/plain", USER_AGENT), self.requests)

    def test_real_probe_timeout_keeps_status(self):
        with self.assertRaisesRegex(probes.ProbeError, "timeout"):
            probes.probe_public_ip(self.base + "/slow", .5)

    def route_config(self, paths):
        cfg = copy.deepcopy(DEFAULT)
        cfg["route"].update(public_ip_urls=[self.base + path for path in paths],
                             expected_public_cidrs=["192.0.2.1/32"], source_timeout=3)
        return cfg

    @unittest.skipUnless(shutil.which("ip"), "Real route lookup requires iproute2 (Linux host/Core CI)")
    def test_full_guard_child_uses_header_and_falls_back(self):
        cfg = self.route_config(["/http-error", "/json", "/vpn"])
        result = probes.guard(cfg, "127.0.0.1")
        self.assertTrue(result["verified"])
        self.assertTrue(result["allow_measurement"])
        self.assertEqual(result["sources"][0]["reason"], "http_error")
        self.assertEqual(result["sources"][1]["ip"], "192.0.2.1")
        self.assertEqual(result["sources"][2]["status"], "skipped")
        self.assertEqual(self.requests, [("/http-error", USER_AGENT), ("/json", USER_AGENT)])

    @unittest.skipUnless(shutil.which("ip"), "Real route lookup requires iproute2")
    def test_guard_slow_interpreter_startup_keeps_fast_three_second_witness(self):
        starts = self.child_environment(startup_delay=3.6)
        result = probes.guard(self.route_config(["/json"]), "127.0.0.1")
        self.assertTrue(result["verified"])
        self.assertEqual(starts.read_text().splitlines(), ["started"])
        self.assertEqual(self.requests, [("/json", USER_AGENT)])

    @unittest.skipUnless(shutil.which("ip"), "Real route lookup requires iproute2")
    def test_stalled_dns_allows_fallback_and_does_not_delay_guard_exit(self):
        self.child_environment(stalled_dns=True)
        cfg = self.route_config(["/json"])
        cfg["route"].update(public_ip_urls=["https://stalled.example.test/ip", self.base + "/json"], source_timeout=1)
        started = time.monotonic()
        result = probes.guard(cfg, "127.0.0.1", timeout=5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(result["verified"])
        self.assertEqual(result["sources"][0]["reason"], "timeout")
        self.assertEqual(result["sources"][1]["ip"], "192.0.2.1")

    @unittest.skipUnless(shutil.which("ip"), "Real route lookup requires iproute2")
    def test_startup_counts_towards_deadline_with_structured_witness_errors(self):
        self.child_environment(startup_delay=0.6, stalled_dns=True)
        cfg = self.route_config(["/json"])
        cfg["route"].update(public_ip_urls=["https://stalled.example.test/ip"], source_timeout=3)
        started = time.monotonic()
        with self.assertRaisesRegex(probes.ProbeError, "route_verification_unavailable") as error:
            probes.guard(cfg, "127.0.0.1", timeout=1.5)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(error.exception.details["sources"][0]["reason"], "timeout")
        self.assertFalse(error.exception.details["allow_measurement"])

    @unittest.skipUnless(shutil.which("ip"), "Real route lookup requires iproute2")
    def test_guard_hard_deadline_includes_unbounded_startup(self):
        self.child_environment(startup_delay=30)
        started = time.monotonic()
        with self.assertRaisesRegex(probes.ProbeError, "route_verification_unavailable") as error:
            probes.guard(self.route_config(["/json"]), "127.0.0.1", timeout=0.5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(error.exception.details["verified"])
        self.assertEqual(error.exception.details["sources"][0]["reason"], "guard_process_failed_or_timed_out")

    @unittest.skipUnless(shutil.which("ip") and shutil.which("iperf3"), "Real measurement requires iproute2 and iperf3")
    def test_complete_local_iperf_measurement_with_required_guard_and_timeout_fallback(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        server = subprocess.Popen(["iperf3", "-s", "-B", "127.0.0.1", "-p", str(port)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: server.wait(timeout=5))
        self.addCleanup(server.terminate)
        for attempt in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if server.poll() is not None:
                    self.fail("Local iperf3 server exited")
                time.sleep(0.02)
        else:
            self.fail("Local iperf3 server did not start")
        cfg = self.route_config(["/slow", "/json"])
        cfg.update(seconds=1, parallel=1, min_interval=0, retry_delay=0, reserve_every=100)
        cfg["route"]["source_timeout"] = 1
        template = cfg["server_groups"]["primary"]["servers"][0]
        cfg["server_groups"]["primary"]["servers"] = [dict(template, id="local-iperf-test", host="127.0.0.1",
                                                           ports=[port], min_download_mbps=0, min_upload_mbps=0)]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            result = Runner(cfg, store).run()
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(store.latest()["id"], result["id"])
        sample = result["primary"]
        self.assertTrue(sample["valid"])
        self.assertGreater(sample["download"]["mbps"], 0)
        self.assertGreater(sample["upload"]["mbps"], 0)
        self.assertEqual(len(sample["route_checks"]), 4)
        for check in sample["route_checks"]:
            self.assertTrue(check["verified"])
            self.assertEqual(check["sources"][0]["reason"], "timeout")
            self.assertEqual(check["sources"][1]["ip"], "192.0.2.1")

    @unittest.skipUnless(shutil.which("ip"), "Real route lookup requires iproute2 (Linux host/Core CI)")
    def test_full_guard_mismatch_and_unavailable_fail_closed(self):
        for path, reason in (("/vpn", "route_public_ip_mismatch"), ("/http-error", "route_verification_unavailable")):
            with self.subTest(path=path), self.assertRaisesRegex(probes.ProbeError, reason) as error:
                probes.guard(self.route_config([path]), "127.0.0.1")
            self.assertFalse(error.exception.details["allow_measurement"])

