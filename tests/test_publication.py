"""Regression tests for credentials hidden inside publication artifacts."""
import io
from pathlib import Path
import tarfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / 'tools/check_publication.py').is_file()
if HAS_TOOLS:
    from tools.check_publication import inspect


@unittest.skipUnless(HAS_TOOLS, 'Publication tooling is not in the runtime image')
class PublicationTests(unittest.TestCase):
    def test_nested_archive_cannot_hide_credentials(self):
        token = ('gh' + 'p_' + 'X' * 36).encode()
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode='w:gz') as archive:
            member = tarfile.TarInfo('stolas-fixture/notes.txt')
            member.size = len(token)
            archive.addfile(member, io.BytesIO(token))
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('tests/fixtures/nested.tar.gz', inner.getvalue())
        findings = inspect(outer.getvalue(), 'tests/fixtures/outer.zip')
        self.assertTrue(any('notes.txt' in path and reason == 'credential pattern' for path, reason in findings))
        self.assertNotIn(token.decode(), repr(findings))

    def test_production_data_and_unreviewed_hosts_are_rejected(self):
        self.assertTrue(inspect(b'{}', 'config/local.json'))
        self.assertTrue(inspect(b'https://' + b'private-endpoint.corp', 'README.md'))
        self.assertTrue(inspect(b'8.8.' + b'4.4', 'docs/sample.md'))
        self.assertFalse(inspect(b'https://api.github.com', 'tools/download.py'))
        self.assertFalse(inspect(b'203.0.113.10', 'docs/sample.md'))

    def test_only_exact_public_test_key_is_exempt(self):
        data = (ROOT / 'tests/fixtures/localhost-test-key.pem').read_bytes()
        self.assertFalse(inspect(data, 'tests/fixtures/localhost-test-key.pem'))
        self.assertTrue(inspect(data + b'\n', 'tests/fixtures/localhost-test-key.pem'))
        self.assertTrue(inspect(data, 'config/server.pem'))

    def test_malformed_archive_fails_closed(self):
        self.assertTrue(inspect(b'not a tar', 'tests/fixtures/broken.tar.gz'))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            member = tarfile.TarInfo('stolas-fixture/link')
            member.type = tarfile.SYMTYPE
            member.linkname = '/outside'
            archive.addfile(member)
        self.assertTrue(inspect(output.getvalue(), 'tests/fixtures/link.tar.gz'))
