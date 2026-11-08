"""Offline regressions for APK preflight and lock generation."""
import contextlib
import hashlib
import http.client
import io
import json
import socket
import ssl
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/lock_apk.py").is_file()
if HAS_TOOLS:
    from tools import lock_apk


@unittest.skipUnless(HAS_TOOLS, "Maintainer tools are outside the production image")
class ApkLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = b"synthetic APK payload"
        self.lock = {"base": "alpine:test@sha256:" + "a" * 64, "architectures": {}}
        for arch in lock_apk.ARCHITECTURES:
            self.lock["architectures"][arch] = [{
                "name": "test-package", "version": "1-r0",
                "url": f"https://example.test/{arch}/test-package-1-r0.apk",
                "sha256": hashlib.sha256(self.payload).hexdigest(),
            }]
        lock_apk.write_lock(self.lock, self.root)
        (self.root / "Dockerfile").write_text("FROM " + self.lock["base"] + "\n")

    def snapshot(self):
        return {p.name: p.read_bytes() for p in (self.root / "config").iterdir()}

    def test_http_404_reports_exact_url_and_preserves_pins(self):
        url = self.lock["architectures"]["x86_64"][0]["url"]
        before = self.snapshot()
        with patch.object(lock_apk.urllib.request, "urlopen",
                          side_effect=urllib.error.HTTPError(url, 404, "Not Found", {}, None)) as request, \
                patch.object(lock_apk.time, "sleep") as sleep:
            with self.assertRaises(ValueError) as result:
                lock_apk.check_lock(self.root)
            request.assert_called_once()
            sleep.assert_not_called()
        self.assertIn("HTTP 404", str(result.exception))
        self.assertIn(url, str(result.exception))
        self.assertEqual(self.snapshot(), before)

    def test_tls_or_network_failure_is_not_ignored(self):
        with patch.object(lock_apk.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError(ssl.SSLCertVerificationError("private detail"))) as request, \
                patch.object(lock_apk.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "APK download failed"):
                lock_apk.check_lock(self.root)
            request.assert_called_once()
            sleep.assert_not_called()

    def test_transient_errors_retry_then_succeed(self):
        url = self.lock["architectures"]["x86_64"][0]["url"]
        failures = [urllib.error.URLError(socket.gaierror("temporary DNS failure")),
                    TimeoutError(), ConnectionResetError()]
        failures += [urllib.error.HTTPError(url, code, "temporary error", {}, None)
                     for code in (408, 429, 500, 502, 503, 504)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, code=getattr(failure, "code", None)):
                with patch.object(lock_apk.urllib.request, "urlopen",
                                  side_effect=[failure, io.BytesIO(self.payload)]) as request, \
                        patch.object(lock_apk.time, "sleep") as sleep:
                    self.assertEqual(lock_apk.fetch(url), self.payload)
                    self.assertEqual(request.call_count, 2)
                    sleep.assert_called_once_with(1)

    def test_partial_response_is_discarded_before_retry(self):
        class PartialResponse(io.BytesIO):
            def read(self):
                raise http.client.IncompleteRead(b"partial payload")

        url = self.lock["architectures"]["x86_64"][0]["url"]
        with patch.object(lock_apk.urllib.request, "urlopen",
                          side_effect=[PartialResponse(), io.BytesIO(self.payload)]), \
                patch.object(lock_apk.time, "sleep"):
            self.assertEqual(lock_apk.fetch(url), self.payload)

    def test_persistent_network_failure_is_bounded_and_redacted(self):
        url = self.lock["architectures"]["x86_64"][0]["url"]
        with patch.object(lock_apk.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError(ConnectionResetError("private-detail"))) as request, \
                patch.object(lock_apk.time, "sleep") as sleep:
            with self.assertRaises(ValueError) as result:
                lock_apk.fetch(url)
            self.assertEqual(request.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertNotIn("private-detail", str(result.exception))

    def test_credential_and_query_urls_rejected_without_logging_or_requests(self):
        for suffix in ("?token=synthetic-secret", "#synthetic-secret"):
            url = self.lock["architectures"]["x86_64"][0]["url"] + suffix
            with patch.object(lock_apk.urllib.request, "urlopen") as request:
                with self.assertRaises(ValueError) as result:
                    lock_apk.fetch(url)
                request.assert_not_called()
                self.assertNotIn("synthetic-secret", str(result.exception))
        url = "https://" + "user:synthetic-secret@" + "example.test/file.apk"
        with patch.object(lock_apk.urllib.request, "urlopen") as request:
            with self.assertRaises(ValueError) as result:
                lock_apk.fetch(url)
            request.assert_not_called()
            self.assertNotIn("synthetic-secret", str(result.exception))

    def test_changed_payload_fails_sha256_without_rewriting_lock(self):
        before = self.snapshot()
        with patch.object(lock_apk, "fetch", return_value=b"changed upstream bytes"):
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch: x86_64/test-package"):
                lock_apk.check_lock(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_interrupted_publication_restores_every_original(self):
        before = self.snapshot()
        changed = json.loads(json.dumps(self.lock))
        changed["architectures"]["x86_64"][0]["sha256"] = "b" * 64
        replace = Path.replace
        for fail_at in (2, 3):
            for failure in (OSError, KeyboardInterrupt):
                with self.subTest(fail_at=fail_at, failure=failure):
                    calls = 0

                    def fail_once(source, target):
                        nonlocal calls
                        calls += 1
                        if calls == fail_at:
                            raise failure("synthetic failure")
                        return replace(source, target)

                    with patch.object(Path, "replace", fail_once):
                        with self.assertRaises(failure):
                            lock_apk.write_lock(changed, self.root)
                    self.assertEqual(self.snapshot(), before)

    def test_failed_rollback_retains_backups_and_blocks_further_use(self):
        before = self.snapshot()
        replace = Path.replace
        calls = 0

        def fail_after_first(source, target):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("private-detail")
            return replace(source, target)

        with patch.object(Path, "replace", fail_after_first):
            with self.assertRaisesRegex(ValueError, "backups retained") as result:
                lock_apk.write_lock(self.lock, self.root)
        self.assertNotIn("private-detail", str(result.exception))
        transaction = self.root / "config" / lock_apk.UPDATE_DIRECTORY
        self.assertEqual({p.name: p.read_bytes() for p in (transaction / "original").iterdir()}, before)
        with patch.object(lock_apk, "fetch") as fetch:
            with self.assertRaisesRegex(ValueError, "in progress or incomplete"):
                lock_apk.check_lock(self.root)
            fetch.assert_not_called()
        with self.assertRaisesRegex(ValueError, "in progress or incomplete"):
            lock_apk.write_lock(self.lock, self.root)

    def test_failed_resolution_never_publishes_partial_lock(self):
        names = ("python3", "iperf3", "iproute2-minimal", "ca-certificates",
                 "ca-certificates-bundle", "alpine-baselayout", "alpine-keys",
                 "apk-tools", "busybox-binsh", "musl-utils", "scanelf", "ssl_client")
        index = "\n\n".join(f"P:{name}\nV:1-r0" for name in names).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("APKINDEX")
            member.size = len(index)
            archive.addfile(member, io.BytesIO(index))

        def fetch(url):
            if "/aarch64/" in url:
                raise ValueError("aarch64 download failed")
            return buffer.getvalue() if url.endswith("APKINDEX.tar.gz") else self.payload

        before = self.snapshot()
        with patch.object(lock_apk, "fetch", side_effect=fetch), \
                patch.object(lock_apk, "write_lock") as write, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                lock_apk.main([])
            self.assertEqual(result.exception.code, 1)
            write.assert_not_called()
        self.assertEqual(self.snapshot(), before)
