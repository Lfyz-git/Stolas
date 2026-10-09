"""Small bounded HTTP service intended for a trusted TLS reverse proxy."""
import hmac
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .runner import Cooldown
from .storage import Busy


class BoundedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(10)
        return client, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(runner, address, token):
    if not token or len(token) < 32 or token.startswith("CHANGE") or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("STOLAS_API_TOKEN must be a random ASCII token of at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        server_version = "Stolas"
        sys_version = ""

        def log_message(self, *args):
            pass  # Never log credentials, client addresses or raw request paths.

        def reply(self, code, body):
            data = json.dumps(body, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (OSError, socket.timeout):
                pass

        def authenticated(self):
            actual = self.headers.get("Authorization", "").encode("utf-8")
            if not hmac.compare_digest(actual, ("Bearer " + token).encode()):
                self.reply(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self.authenticated():
                return
            if self.path == "/healthz":
                self.reply(200, {"status": "ready"})
            elif self.path == "/v1/results/latest":
                result = runner.store.latest()
                self.reply(200 if result else 404, result or {"error": "no_results"})
            elif self.path == "/v1/results":
                self.reply(200, {"results": runner.store.history()})
            else:
                self.reply(404, {"error": "not_found"})

        def do_POST(self):
            if not self.authenticated():
                return
            if self.path != "/v1/tests":
                self.reply(404, {"error": "not_found"})
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                self.reply(400, {"error": "request_body_not_allowed"})
                return
            try:
                self.reply(200, runner.run())
            except Busy:
                self.reply(409, {"error": "test_running"})
            except Cooldown:
                self.reply(429, {"error": "cooldown"})
            except Exception:
                self.reply(500, {"error": "internal_error"})

    return BoundedServer(address, Handler)
