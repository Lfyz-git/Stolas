"""Offline tests of curl|sh, TTY prompts, archive handling and handoff."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap.sh"


@unittest.skipUnless(sys.platform.startswith("linux") and BOOTSTRAP.exists(), "Linux download bootstrap outside production image")
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "fixture.tar.gz"
        self.target = self.root / "installation with spaces"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "curl"
        stub.write_text("#!" + sys.executable + "\n" + '''import hashlib, json, os, pathlib, shutil, sys
pathlib.Path(os.environ["STOLAS_TEST_CURL_LOG"]).write_text(json.dumps(sys.argv[1:]))
if os.environ.get("STOLAS_TEST_DOWNLOAD_FAIL"):
    sys.exit(22)
destination = sys.argv[sys.argv.index("--output") + 1]
if sys.argv[-1].endswith("/SHA256SUMS"):
    digest = os.environ.get("STOLAS_TEST_BAD_HASH") or hashlib.sha256(pathlib.Path(os.environ["STOLAS_TEST_ARCHIVE"]).read_bytes()).hexdigest()
    pathlib.Path(destination).write_text(digest + "  stolas-v0.3.0.tar.gz\\n")
else:
    shutil.copyfile(os.environ["STOLAS_TEST_ARCHIVE"], destination)
''')
        stub.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        self.env["STOLAS_TEST_CURL_LOG"] = str(self.root / "curl.json")
        self.env["STOLAS_TEST_ARCHIVE"] = str(self.archive)
        self.env["TMPDIR"] = str(self.root)
        self.make_archive()

    def make_archive(self, extra=None, real=False):
        files = {
            "install.sh": b'#!/bin/sh\ncd "$(dirname "$0")"\nexec python3 tools/deploy.py "$@"\n',
            "tools/deploy.py": (ROOT / "tools/deploy.py").read_bytes(),
            "tools/environment.py": (ROOT / "tools/environment.py").read_bytes(),
            "tools/install.py": b'''import pathlib, sys, os
if '--diagnose' in sys.argv:
    print('read-only diagnostics')
    sys.exit(0)
print('Wizard answer: ', end='', flush=True)
answer = input()
pathlib.Path('wizard-answer').write_text(answer)
pathlib.Path('wizard-args').write_text(' '.join(sys.argv[1:]))
sys.exit(int(os.getenv('STOLAS_TEST_WIZARD_EXIT', '0')))
''',
            "fixture-unused.sh": b'''#!/bin/sh
printf 'Wizard answer: '
IFS= read -r answer
printf '%s' "$answer" > wizard-answer
printf '%s\\n' "$@" > wizard-args
exit "${STOLAS_TEST_WIZARD_EXIT:-0}"
''',
            "agent/config.py": b"# fixture\n",
            "compose.yaml": b"services: {}\n",
            "config/example.json": b"{}\n",
        }
        if real:
            for name in ("install.sh", "tools/install.py", "tools/deploy.py", "tools/environment.py", "n8n/stolas.json", "compose.yaml", "config/example.json"):
                files[name] = (ROOT / name).read_bytes()
            for path in (ROOT / "agent").glob("*.py"):
                files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo("Stolas-test/" + name)
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
            if extra:
                archive.addfile(extra)

    def pipeline(self, args=None, answers="hello\n"):
        # A real controlling terminal is necessary to verify /dev/tty handoff.
        import fcntl
        import pty
        import select
        import termios
        master, slave = pty.openpty()
        command = "cat " + shlex.quote(str(BOOTSTRAP)) + " | sh -s -- " + shlex.join(args or ["--dir", str(self.target)])
        process = subprocess.Popen(["sh", "-c", command], cwd=self.root, env=self.env,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                   preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        output = bytearray()
        try:
            os.write(master, answers.encode())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output.extend(data)
                elif process.poll() is not None:
                    break
            code = process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        return code, output.decode("utf-8", errors="replace")

    def assert_no_staging(self):
        self.assertEqual(list(self.root.glob("stolas-download.*")), [])

    def assert_diagnostics(self):
        folders = list(self.root.glob("stolas-download.*"))
        self.assertTrue(folders)
        self.assertTrue(all((folder / "diagnostic.txt").is_file() for folder in folders))

    def test_pipeline_retains_interactive_input_and_supports_spaces(self):
        code, output = self.pipeline()
        self.assertEqual(code, 0, output)
        self.assertEqual((self.target / "wizard-answer").read_text(), "hello")
        self.assertFalse((self.target / ".git").exists())
        self.assertTrue((self.target / "compose.yaml").exists())
        self.assert_no_staging()

    def test_directory_prompt_ref_and_wizard_arguments(self):
        code, output = self.pipeline(["--ref", "v0.3.0", "--configure-only"], str(self.target) + "\nanswer\n")
        self.assertEqual(code, 0, output)
        self.assertEqual((self.target / "wizard-answer").read_text(), "answer")
        self.assertIn("--configure-only", (self.target / "wizard-args").read_text())
        call = json.loads((self.root / "curl.json").read_text())
        self.assertEqual(call[-1], "https://github.com/Lfyz-git/Stolas/releases/download/v0.3.0/stolas-v0.3.0.tar.gz")

    def test_existing_installation_is_not_overwritten(self):
        self.target.mkdir()
        secret = self.target / ".env"
        secret.write_text("keep-me")
        code, output = self.pipeline()
        self.assertEqual(code, 1, output)
        self.assertEqual(secret.read_text(), "keep-me")
        self.assert_diagnostics()

    def test_download_failure_does_not_create_installation(self):
        self.env["STOLAS_TEST_DOWNLOAD_FAIL"] = "1"
        code, output = self.pipeline()
        self.assertEqual(code, 1, output)
        self.assertFalse(self.target.exists())
        self.assertIn("Не удалось", output)
        self.assert_diagnostics()

    def test_corrupt_archive_does_not_create_installation(self):
        self.archive.write_bytes(b"not a gzip archive")
        code, output = self.pipeline()
        self.assertEqual(code, 1, output)
        self.assertFalse(self.target.exists())
        self.assert_diagnostics()

    def test_archive_path_traversal_and_symlinks_are_rejected(self):
        for unsafe in (tarfile.TarInfo("Stolas-test/../../escaped"), tarfile.TarInfo("Stolas-test/link")):
            if unsafe.name.endswith("/link"):
                unsafe.type = tarfile.SYMTYPE
                unsafe.linkname = "../../escaped"
            self.make_archive(unsafe)
            code, output = self.pipeline()
            self.assertEqual(code, 1, output)
            self.assertFalse(self.target.exists())
            self.assertFalse((self.root / "escaped").exists())
            self.assert_diagnostics()

    def test_wizard_exit_status_is_preserved_and_files_remain_for_retry(self):
        self.env["STOLAS_TEST_WIZARD_EXIT"] = "2"
        code, output = self.pipeline()
        self.assertEqual(code, 2, output)
        self.assertTrue((self.target / "install.sh").exists())
        self.assert_diagnostics()

    def test_existing_empty_directory_is_accepted(self):
        self.target.mkdir()
        code, output = self.pipeline()
        self.assertEqual(code, 0, output)
        self.assertEqual((self.target / "wizard-answer").read_text(), "hello")
        self.assert_no_staging()

    def test_real_wizard_cancel_then_retry_without_removing_target(self):
        self.make_archive(real=True)
        for attempt in range(2):
            code, output = self.pipeline(answers=":cancel\n")
            self.assertEqual(code, 3, output)
            self.assertFalse((self.target / ".env").exists())
            self.assertFalse((self.target / "config/local.json").exists())
            self.assertEqual(list(self.target.rglob("*.pyc")), [])
        code, output = self.pipeline(["--dir", str(self.target), "--configure-only"], "off\nlater\napply\n")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads((self.target / "config/local.json").read_text())["route"]["mode"], "off")
        self.assertFalse((self.target / ".stolas-draft.json").exists())

    def test_installed_directory_offers_reconfigure_and_update(self):
        self.assertEqual(self.pipeline()[0], 0)
        for action in ("reconfigure", "update"):
            code, output = self.pipeline(answers=action + "\nsecond\n")
            self.assertEqual(code, 0, output)
            self.assertIn("Stolas уже установлен", output)
            self.assertEqual((self.target / "wizard-answer").read_text(), "second")

    def test_bad_release_checksum_never_extracts_or_runs_wizard(self):
        self.env["STOLAS_TEST_BAD_HASH"] = "0" * 64
        code, output = self.pipeline()
        self.assertEqual(code, 1, output)
        self.assertIn("Контрольная сумма", output)
        self.assertFalse(self.target.exists())
        self.assert_diagnostics()

    def test_explicit_sha_checksum_and_ref(self):
        import hashlib
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        code, output = self.pipeline(["--dir", str(self.target), "--ref", "a" * 40, "--sha256", digest])
        self.assertEqual(code, 0, output)
        self.assertIn("codeload.github.com", json.loads((self.root / "curl.json").read_text())[-1])

    def test_missing_tty_fails_with_actionable_error(self):
        command = "cat " + shlex.quote(str(BOOTSTRAP)) + " | sh -s -- --dir " + shlex.quote(str(self.target))
        result = subprocess.run(["sh", "-c", command], cwd=self.root, env=self.env,
                                capture_output=True, text=True, start_new_session=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("интерактивный терминал", result.stderr)
        self.assertFalse(self.target.exists())

    def test_diagnostics_without_tty_do_not_create_target_or_parent(self):
        target = self.root / "absent-parent" / "stolas"
        result = subprocess.run(["sh", str(BOOTSTRAP), "--dir", str(target), "--diagnose"], cwd=self.root, env=self.env,
                                capture_output=True, text=True, start_new_session=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("read-only diagnostics", result.stdout)
        self.assertFalse(target.parent.exists())

    def test_truncated_bootstrap_body_never_starts_installation(self):
        script = BOOTSTRAP.read_text(encoding="utf-8")
        script = script[:script.index("    # Validate the GitHub archive")]
        result = subprocess.run(["sh", "-s", "--", "--dir", str(self.target)], input=script,
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / "curl.json").exists())


if __name__ == "__main__":
    unittest.main()
