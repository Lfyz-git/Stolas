"""Synthetic endpoint choices and real local HTTP/TLS transport boundaries."""
from pathlib import Path
import socket
import ssl
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/integrate.py").is_file()
if HAS_TOOLS:
    from tools import integrate, install


@unittest.skipUnless(HAS_TOOLS, "Integration wizard is not shipped in Core image")
class TransportTests(unittest.TestCase):
    def test_integration_validator_does_not_relax_other_url_policies(self):
        with self.assertRaises(ValueError):
            install.url("http://service.example.test")
        for value in ("ftp://service.example.test", "http://service.example.test:0",
                      "http://service.example.test:70000", "http://service.example.test:",
                      "http://service.example.test:abc", "http://[broken", "http://service.example.test/?q=1",
                      "http://" + "@service.example.test", "http://" + ":@service.example.test",
                      "http://service.example.test/#fragment", "http://service.example.test/\x00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                integrate.connection_url(value)

    def test_fixed_network_diagnostics_do_not_expose_exception_payloads(self):
        for reason, expected in ((socket.gaierror("private-payload"), "DNS"),
                                 (ConnectionRefusedError("private-payload"), "отклонил"),
                                 (TimeoutError("private-payload"), "время ожидания"),
                                 (ssl.SSLError("private-payload"), "TLS-соединение")):
            with self.subTest(reason=type(reason).__name__), patch.object(install.urllib.request.OpenerDirector, "open", side_effect=urllib.error.URLError(reason)):
                with self.assertRaises(install.ServiceRequestError) as caught:
                    install.request_json("https://service.example.test", "/healthz", "fixture-only-key")
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("private-payload", str(caught.exception))
                self.assertNotIn("fixture-only-key", str(caught.exception))

    def server(self, tls=False):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization"), self.headers.get("X-N8N-API-KEY")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/destination")
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
        server = HTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(ROOT / "tests/fixtures/localhost-test-cert.pem", ROOT / "tests/fixtures/localhost-test-key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port), received

    def test_actual_redirects_never_forward_either_key(self):
        base, received = self.server()
        for n8n in (False, True):
            with self.assertRaises(install.ServiceRequestError) as caught:
                install.request_json(base, "/redirect", "fixture-only-key", n8n=n8n)
            self.assertIn("не перенаправлен", str(caught.exception))
        self.assertEqual([item[0] for item in received], ["/redirect", "/redirect"])
        self.assertEqual(received[0][1:], ("Bearer fixture-only-key", None))
        self.assertEqual(received[1][1:], (None, "fixture-only-key"))

    def test_actual_untrusted_https_certificate_rejected_before_credentials(self):
        base, received = self.server(tls=True)
        with self.assertRaises(install.ServiceRequestError) as caught:
            install.request_json(base, "/healthz", "fixture-only-key", n8n=True)
        self.assertIn("Сертификат HTTPS не прошёл проверку", str(caught.exception))
        self.assertEqual(received, [])
