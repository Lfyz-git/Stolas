"""Synthetic endpoint choices and real local HTTP/TLS transport boundaries."""
import contextlib
import io
from pathlib import Path
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/integrate.py").is_file()
if HAS_TOOLS:
    from tools import integrate, install


@unittest.skipUnless(HAS_TOOLS, "Integration wizard is not shipped in Core image")
class TransportTests(unittest.TestCase):
    def test_http_consent_is_shared_and_precedes_keys_requests_and_writes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("STOLAS_LISTEN=127.0.0.1\nSTOLAS_PORT=8080\nSTOLAS_API_TOKEN=" + "a" * 40 + "\n")
            original = (root / ".env").read_bytes()
            facts = dict(docker={"available": True}, stolas=[], n8n=[], addresses=[], timezone="Etc/UTC")
            answers = ["native", "123", "", "", "http://n8n.example.test", ""]
            with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(integrate.environment, "discover", return_value=facts), patch.object(integrate.environment, "port_state", return_value="free"), patch("builtins.input", side_effect=answers), patch("getpass.getpass") as key, patch.object(integrate, "request_json") as request, self.assertRaises(install.Cancel):
                integrate.integrate(root, "api")
            key.assert_not_called(); request.assert_not_called()
            self.assertEqual((root / ".env").read_bytes(), original)
            self.assertFalse((root / "n8n").exists())
            self.assertIn("HTTP не шифрует", output.getvalue())
            self.assertIn("Stolas, n8n", output.getvalue())
        options = dict(mode="api", endpoint="http://127.0.0.1:8080", url="http://n8n.example.test")
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.input", return_value="trusted") as prompt:
            integrate.confirm_transport(options)
        prompt.assert_called_once()
        with patch("builtins.input") as prompt:
            integrate.confirm_transport(dict(mode="api", endpoint="https://stolas.example.test", url="https://n8n.example.test"))
        prompt.assert_not_called()

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
