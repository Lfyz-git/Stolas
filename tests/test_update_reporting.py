"""Installed-version and update output, including a failed cleanup after commit."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/manage.py").is_file()
if HAS_TOOLS:
    from tools import deploy, install, layout, manage


@unittest.skipUnless(HAS_TOOLS, "Host management outside Core image")
class VersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "installation"
        self.root.mkdir()

    def manifest(self, data, name=".stolas/state/managed.json"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def cli(self, action="version"):
        output = io.StringIO()
        with patch("sys.argv", ["stolas", "--root", str(self.root), action]), contextlib.redirect_stdout(output), patch.object(manage.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")), patch.object(manage.resources, "command", side_effect=AssertionError("Docker forbidden")), patch.object(manage.subprocess, "run", side_effect=AssertionError("subprocess forbidden")):
            code = manage.main()
        return code, output.getvalue()

    def test_installed_release_is_local_and_independent_of_manager_version(self):
        self.manifest({"ref": "v0.5.0", "management_version": layout.VERSION})
        self.assertEqual(self.cli(), (0, "Stolas Core v0.5.0\n"))

    def test_missing_invalid_or_unreadable_metadata_is_graceful_without_writes(self):
        for value in (None, [], {"ref": "private-text-never-print"}):
            with self.subTest(value=value):
                if value is not None:
                    self.manifest(value)
                before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
                code, text = self.cli()
                self.assertEqual(code, 1)
                self.assertIn("Не удалось определить", text)
                self.assertNotIn("private-text-never-print", text)
                self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.manifest({"ref": "v0.5.0"})
        with patch.object(layout.Path, "read_text", side_effect=PermissionError("private-path")):
            code, text = self.cli()
        self.assertEqual(code, 1)
        self.assertNotIn("private-path", text)

    def test_malformed_json_is_graceful(self):
        self.manifest({})
        (self.root / ".stolas/state/managed.json").write_text("not-json")
        self.assertEqual(self.cli()[0], 1)

    def test_legacy_manifest_and_nonrelease_compose_metadata(self):
        self.manifest({"ref": "v0.4.0"}, ".stolas-managed.json")
        self.assertEqual(layout.installed_version(self.root), "v0.4.0")
        self.manifest({"ref": "checkout"})
        (self.root / "compose.yaml").write_text("services:\n  stolas:\n    image: stolas:0.5.0\n")
        self.assertEqual(layout.installed_version(self.root), "v0.5.0")

    def test_uncommitted_installation_does_not_claim_a_staged_release(self):
        self.manifest({"ref": "v0.6.3"})
        for phase in ("prepared", "installed", "interrupted"):
            self.manifest({"phase": phase}, ".stolas/state/transaction.json")
            self.assertIsNone(layout.installed_version(self.root))
        self.manifest({"phase": "rolled_back"}, ".stolas/state/transaction.json")
        self.assertEqual(layout.installed_version(self.root), "v0.6.3")

    def test_version_appears_in_help_without_docker_or_network(self):
        code, text = self.cli("help")
        self.assertEqual(code, 0)
        self.assertIn("version", text)
        self.assertIn("update", text)

    @unittest.skipIf(os.name == "nt", "POSIX link safety")
    def test_linked_manifest_is_not_read(self):
        outside = self.root.parent / "outside.json"
        outside.write_text('{"ref":"v9.9.9"}')
        directory = self.root / ".stolas/state"
        directory.mkdir(parents=True)
        (directory / "managed.json").symlink_to(outside)
        self.assertIsNone(layout.installed_version(self.root))


@unittest.skipUnless(HAS_TOOLS, "Host management outside Core image")
class UpdateReportingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "installation"
        with patch.object(deploy, "run_installer", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            deploy.deploy(ROOT, self.root, ref="v0.6.3", configure_only=True)
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/local.json").write_text("{}")
        (self.root / ".env").write_text("synthetic")
        self.output = io.StringIO()
        for item in (patch.object(install, "require_docker_access"),
                     patch("tools.resources.stop_replacement", return_value=None),
                     contextlib.redirect_stdout(self.output)):
            item.__enter__()
            self.addCleanup(item.__exit__, None, None, None)

    def update(self, code=0):
        with patch.object(deploy, "run_installer", return_value=code):
            return deploy.deploy(ROOT, self.root, ref="v0.6.4", action="update")

    def test_success_is_confirmed_before_separate_restore_point_report(self):
        self.assertEqual(self.update(), 0)
        text = self.output.getvalue()
        self.assertIn("Stolas успешно обновлён до v0.6.4", text)
        self.assertLess(text.index("успешно обновлён"), text.index("Точки восстановления"))
        self.assertEqual(deploy.read_json(self.root / ".stolas/state/transaction.json")["phase"], "complete")
        self.assertEqual(layout.installed_version(self.root), "v0.6.4")

    def test_cleanup_failure_does_not_change_update_success_or_advise_repetition(self):
        for _ in range(3):
            self.update()
        self.output.seek(0)
        self.output.truncate()
        with patch.object(deploy.shutil, "rmtree", side_effect=PermissionError("private-path")):
            deploy.shutil.rmtree.avoids_symlink_attacks = True
            self.assertEqual(self.update(), 0)
        text = " ".join(self.output.getvalue().split())
        self.assertIn("Stolas успешно обновлён до v0.6.4", text)
        self.assertIn("не удалось удалить", text)
        self.assertNotRegex(text, r"повтор.*обнов|private-path|Обновление не выполнено")

    def test_failed_installer_reports_failure_and_restores_old_release(self):
        def fail(*args, **kwargs):
            print("Ошибка: API недоступен")
            (self.root / "config/local.json").write_text("changed")
            return 1
        with patch.object(deploy, "run_installer", side_effect=fail):
            self.assertEqual(deploy.deploy(ROOT, self.root, ref="v0.6.4", action="update"), 1)
        text = self.output.getvalue()
        self.assertIn("API недоступен", text)
        self.assertIn("Обновление не выполнено", text)
        self.assertNotIn("успешно обновлён", text)
        self.assertEqual((self.root / "config/local.json").read_text(), "{}")
        self.assertEqual(layout.installed_version(self.root), "v0.6.3")

    def test_cancel_and_interruption_never_claim_success(self):
        for code, word in ((3, "отменено"), (130, "прервано")):
            self.output.seek(0)
            self.output.truncate()
            self.assertEqual(self.update(code), code)
            text = self.output.getvalue()
            self.assertIn("Обновление " + word, text)
            self.assertNotIn("успешно обновлён", text)
            self.assertNotIn("Удалено устаревших", text)

    def test_finalize_error_preserves_real_reason_and_does_not_claim_success(self):
        with patch.object(deploy, "run_installer", return_value=0), patch.object(deploy, "finalize", side_effect=RuntimeError("finalization failed")):
            with self.assertRaisesRegex(RuntimeError, "finalization failed"):
                deploy.deploy(ROOT, self.root, ref="v0.6.4", action="update")
        self.assertNotIn("успешно обновлён", self.output.getvalue())
        self.assertIn("Обновление не выполнено", self.output.getvalue())

    def test_success_requires_committed_state(self):
        deploy.atomic_json(self.root / ".stolas/state/transaction.json", {"phase": "installed"})
        with self.assertRaisesRegex(RuntimeError, "не зафиксированы"):
            deploy.update_result(self.root, 0)
        self.assertNotIn("успешно обновлён", self.output.getvalue())

    def test_success_requires_final_installer_status(self):
        deploy.atomic_json(self.root / ".stolas/state/status.json", {"stage": "interrupted", "exit_code": 0})
        with self.assertRaisesRegex(RuntimeError, "не зафиксированы"):
            deploy.update_result(self.root, 0)
        self.assertNotIn("успешно обновлён", self.output.getvalue())

    def test_target_version_selection_is_still_independent(self):
        with patch.object(install, "require_docker_access"), patch.object(manage.urllib.request, "urlopen", return_value=io.BytesIO(b"fixture bootstrap")) as fetch, patch.object(manage.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as command:
            self.assertEqual(manage.update(self.root, "v0.5.0"), 1)
        self.assertIn("/v0.5.0/bootstrap.sh", fetch.call_args.args[0])
        self.assertIn("v0.5.0", command.call_args.args[0])
        self.assertIn("Обновление не завершено", self.output.getvalue())
