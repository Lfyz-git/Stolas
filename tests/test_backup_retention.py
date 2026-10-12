"""Synthetic transaction snapshots; no daemon or production state is used."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/deploy.py").is_file()
if HAS_TOOLS:
    from tools import deploy, layout


@unittest.skipUnless(HAS_TOOLS, "Deployment tools are not part of the Core image")
class BackupRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = Path(temporary.name) / "installation"
        self.target.mkdir()
        layout.initialize(self.target)
        self.backups = self.target / deploy.BACKUP_ROOT
        self.backups.mkdir(parents=True, exist_ok=True)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def snapshot(self, number, status="complete", sequence=None):
        path = self.backups / ("20260101T000000Z-%08x" % number)
        (path / "files").mkdir(parents=True)
        (path / "files/compose.yaml").write_text("snapshot-%s" % number)
        state = dict(files={"compose.yaml": True}, modes={}, new_hashes={}, previous_manifest={}, legacy=[], configured=True,
                     created_ns=number if sequence is None else sequence, retention_status=status)
        deploy.atomic_json(path / "state.json", state)
        return path

    def journal(self, path, phase="complete"):
        deploy.atomic_json(self.target / ".stolas/state/transaction.json",
                           {"backup": path.relative_to(self.target).as_posix(), "phase": phase, "configured": True})

    def prune(self):
        deploy.retain_transaction_backups(self.target)

    def names(self):
        return {path.name for path in self.backups.iterdir()}

    def test_zero_through_six_completed_and_idempotence(self):
        for count in range(7):
            with self.subTest(count=count):
                for path in self.backups.iterdir():
                    shutil.rmtree(path)
                paths = [self.snapshot(number) for number in range(count)]
                self.prune()
                self.assertEqual(self.names(), {path.name for path in paths[-3:]})
                self.prune()
                self.assertEqual(self.names(), {path.name for path in paths[-3:]})

    def test_sequence_order_ignores_mtime_and_breaks_ties_by_name(self):
        paths = [self.snapshot(number, sequence=5) for number in range(5)]
        for index, path in enumerate(reversed(paths)):
            os.utime(path, (index + 100, index + 100))
        self.prune()
        self.assertEqual(self.names(), {path.name for path in paths[-3:]})

    def test_pending_and_interrupted_are_additional_safety_slots(self):
        completed = [self.snapshot(number) for number in range(5)]
        pending = self.snapshot(10, "pending")
        interrupted = self.snapshot(11, "pending")
        self.journal(interrupted, "interrupted")
        self.prune()
        self.assertEqual(self.names(), {path.name for path in [*completed[-3:], pending, interrupted]})
        deploy.restore(self.target, interrupted)
        self.assertEqual((self.target / "compose.yaml").read_text(), "snapshot-11")

    def test_protected_completed_occupies_one_slot_and_remains_usable(self):
        paths = [self.snapshot(number) for number in range(6)]
        self.journal(paths[0], "rolled_back")
        self.prune()
        self.assertEqual(self.names(), {path.name for path in [paths[0], *paths[-2:]]})
        deploy.restore(self.target, paths[0])
        self.assertEqual((self.target / "compose.yaml").read_text(), "snapshot-0")

    def test_unclassified_legacy_snapshots_are_preserved(self):
        paths = [self.snapshot(number) for number in range(5)]
        state = deploy.read_json(paths[0] / "state.json")
        state.pop("retention_status")
        state.pop("created_ns")
        deploy.atomic_json(paths[0] / "state.json", state)
        self.prune()
        self.assertTrue(paths[0].is_dir())
        self.assertEqual(self.names(), {path.name for path in [paths[0], *paths[-3:]]})
        self.assertIn("очистка", " ".join(self.output.getvalue().split()))

    def test_legacy_order_is_utc_and_stable(self):
        path = self.snapshot(1)
        state = deploy.read_json(path / "state.json")
        state.pop("created_ns")
        before = deploy.backup_order(path, state)
        os.utime(path, (1, 1))
        self.assertEqual(deploy.backup_order(path, state), before)
        self.assertEqual(before[0], 1767225600000000000)

    def test_unrelated_config_history_and_settings_backups_are_untouched(self):
        originals = {}
        for name in ("config/local.json", ".env", "history.sqlite", ".stolas/backups/settings/saved.json", deploy.BACKUP_ROOT + "/unrelated/data"):
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic-data")
            originals[path] = path.read_bytes()
        for number in range(5):
            self.snapshot(number)
        self.prune()
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_bad_journal_reference_aborts_all_cleanup(self):
        paths = [self.snapshot(number) for number in range(5)]
        for name in ("../outside", ".env", deploy.BACKUP_ROOT + "/unrelated", str(self.target / "outside")):
            deploy.atomic_json(self.target / ".stolas/state/transaction.json", {"backup": name})
            self.prune()
            self.assertEqual(self.names(), {path.name for path in paths})

    def test_symlinks_in_snapshot_and_inventory_are_never_followed(self):
        outside = self.target.parent / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep")
        paths = [self.snapshot(number) for number in range(7)]
        shutil.rmtree(paths[0])
        paths[0].symlink_to(outside, target_is_directory=True)
        (paths[1] / "files/linked").symlink_to(outside, target_is_directory=True)
        state = deploy.read_json(paths[2] / "state.json")
        state["files"]["../outside"] = False
        deploy.atomic_json(paths[2] / "state.json", state)
        (paths[3] / "state.json").unlink()
        (paths[3] / "state.json").symlink_to(sentinel)
        self.prune()
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertTrue(paths[0].is_symlink())
        self.assertTrue(all(path.exists() for path in paths[1:]))

    def test_symlink_root_is_rejected(self):
        outside = self.target.parent / "outside"
        outside.mkdir()
        self.backups.rmdir()
        self.backups.symlink_to(outside, target_is_directory=True)
        self.prune()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("очистка", " ".join(self.output.getvalue().split()))

    def test_missing_snapshot_and_invalid_metadata_are_not_deleted(self):
        paths = [self.snapshot(number) for number in range(6)]
        (paths[0] / "files/compose.yaml").unlink()
        deploy.atomic_json(paths[1] / "state.json", {"files": {}})
        state = deploy.read_json(paths[2] / "state.json")
        state["created_ns"] = "invalid"
        deploy.atomic_json(paths[2] / "state.json", state)
        self.prune()
        self.assertEqual(len(self.names()), 6)

    def test_cleanup_error_warns_without_failing_maintenance(self):
        for number in range(5):
            self.snapshot(number)
        with patch.object(deploy.shutil, "rmtree", side_effect=PermissionError):
            # Mock the platform's explicitly required fd-safe implementation.
            deploy.shutil.rmtree.avoids_symlink_attacks = True
            self.prune()
        self.assertEqual(len(self.names()), 5)
        self.assertIn("очистка", " ".join(self.output.getvalue().split()))
        self.prune()
        self.assertEqual(len(self.names()), 3)

    def test_repeated_update_and_configure_commit_then_retain_and_rollback(self):
        with patch.object(deploy, "run_installer", return_value=0):
            deploy.deploy(ROOT, self.target, configure_only=True)
            (self.target / ".env").write_text("synthetic")
            (self.target / "config").mkdir()
            (self.target / "config/local.json").write_text("{}")
            for index in range(6):
                action = "update" if index % 2 else "reconfigure"
                source = ROOT if action == "update" else self.target
                self.assertEqual(deploy.deploy(source, self.target, action=action, configure_only=True), 0)
                self.assertLessEqual(len(self.names()), 3)
                journal = deploy.read_json(self.target / ".stolas/state/transaction.json")
                self.assertTrue((self.target / journal["backup"] / "state.json").is_file())
            self.assertEqual(deploy.deploy(ROOT, self.target, action="rollback", configure_only=True), 0)
            self.assertEqual((self.target / ".env").read_text(), "synthetic")

    def test_failed_or_interrupted_install_never_prunes(self):
        for number in range(5):
            self.snapshot(number)
        with patch.object(deploy, "run_installer", return_value=130), patch.object(deploy, "retain_transaction_backups") as prune:
            self.assertEqual(deploy.deploy(ROOT, self.target, configure_only=True), 130)
            prune.assert_not_called()
        journal = deploy.read_json(self.target / ".stolas/state/transaction.json")
        self.assertEqual(journal["phase"], "interrupted")
        self.assertEqual(deploy.read_json(self.target / journal["backup"] / "state.json")["retention_status"], "pending")
        self.assertEqual(len(self.names()), 6)

    def test_creation_sequence_advances_when_clock_moves_backwards(self):
        previous = self.snapshot(1, sequence=9999999999999999999)
        backup = deploy.stage_sources(ROOT, self.target, "test", "synthetic")
        self.assertGreater(deploy.read_json(backup / "state.json")["created_ns"], deploy.read_json(previous / "state.json")["created_ns"])

    def legacy(self, path):
        state = deploy.read_json(path / "state.json")
        state.pop("retention_status")
        deploy.atomic_json(path / "state.json", state)

    def receipt(self, holder, referenced, phase="complete", saved=False):
        journal = {"phase": phase, "backup": referenced.relative_to(self.target).as_posix()}
        state = deploy.read_json(holder / "state.json")
        if saved:
            name = ".stolas-transaction.json"
            state["files"][name] = True
            deploy.atomic_json(holder / "files" / name, journal)
        else:
            state["previous_transaction"] = journal
        deploy.atomic_json(holder / "state.json", state)

    def test_seven_mixed_snapshots_reconcile_proven_legacy_and_protect_rollback(self):
        paths = [self.snapshot(n, "complete" if n < 5 else "rolled_back") for n in range(7)]
        for path in paths[:3]:
            self.legacy(path)
        self.receipt(paths[3], paths[0], saved=True)
        self.receipt(paths[4], paths[1], "rolled_back")
        self.journal(paths[6], "rolled_back")
        self.prune()
        self.assertEqual(self.names(), {p.name for p in (paths[2], *paths[4:])})
        text = " ".join(self.output.getvalue().split())
        self.assertEqual(text.count("Резервные копии транзакций:"), 1)
        self.assertIn("всего 4, обычных точек 3/3", text)
        self.assertIn("неизвестных исключений 1", text)
        self.prune()
        self.assertEqual(len(self.names()), 4)

    def test_multiple_unknowns_emit_one_warning_with_counts_and_reason(self):
        paths = [self.snapshot(n) for n in range(7)]
        for path in paths[:3]:
            self.legacy(path)
        self.journal(paths[-1], "rolled_back")
        self.prune()
        text = " ".join(self.output.getvalue().split())
        self.assertEqual(text.count("Резервные копии транзакций:"), 1)
        self.assertIn("всего 6, обычных точек 3/3", text)
        self.assertIn("неизвестных исключений 3", text)
        self.assertIn("подтверждения завершения: 3", text)

    def test_active_legacy_reference_overrides_old_completion_evidence(self):
        paths = [self.snapshot(n) for n in range(6)]
        self.legacy(paths[0])
        self.receipt(paths[1], paths[0])
        self.journal(paths[0], "interrupted")
        self.prune()
        self.assertTrue(paths[0].exists())
        self.assertNotIn("retention_status", deploy.read_json(paths[0] / "state.json"))
        self.assertEqual(len(self.names()), 4)

    def test_legacy_current_rollback_receipt_survives_journal_replacement(self):
        path = self.snapshot(0)
        self.legacy(path)
        self.journal(path, "rolled_back")
        backup = deploy.stage_sources(ROOT, self.target, "fixture", "")
        state = deploy.read_json(backup / "state.json")
        self.assertEqual(state["previous_transaction"]["phase"], "rolled_back")
        self.journal(backup)
        deploy.mark_backup(backup, "complete")
        self.prune()
        self.assertEqual(deploy.read_json(path / "state.json")["retention_status"], "rolled_back")

    def test_untrusted_evidence_never_classifies_by_configured_or_order(self):
        paths = [self.snapshot(n) for n in range(5)]
        for path in paths[:2]:
            self.legacy(path)
        state = deploy.read_json(paths[-1] / "state.json")
        state["previous_transaction"] = {"phase": "complete", "backup": "../outside"}
        deploy.atomic_json(paths[-1] / "state.json", state)
        self.prune()
        self.assertEqual(len(self.names()), 5)
        self.assertIn("некорректное свидетельство", " ".join(self.output.getvalue().split()))

    def test_failed_cleanup_emits_one_report_and_then_converges(self):
        for number in range(7):
            self.snapshot(number)
        with patch.object(deploy.shutil, "rmtree", side_effect=PermissionError):
            deploy.shutil.rmtree.avoids_symlink_attacks = True
            self.prune()
        self.assertEqual(" ".join(self.output.getvalue().split()).count("Резервные копии транзакций:"), 1)
        self.assertIn("обычных точек 7/3", " ".join(self.output.getvalue().split()))
        self.prune()
        self.assertEqual(len(self.names()), 3)

    def test_special_file_and_non_snapshot_entries_are_reported_and_preserved(self):
        if os.name == "nt":
            self.skipTest("Linux FIFO safety")
        paths = [self.snapshot(n) for n in range(5)]
        os.mkfifo(paths[0] / "fifo")
        (self.backups / "unknown-file").write_text("keep")
        self.prune()
        self.assertTrue((paths[0] / "fifo").exists())
        self.assertEqual(len(self.names()), 5)
        self.assertIn("неизвестных исключений 2", " ".join(self.output.getvalue().split()))

    def test_migration_and_cleanup_errors_are_one_operation_report(self):
        paths = [self.snapshot(n) for n in range(5)]
        self.legacy(paths[0])
        self.journal(paths[0])
        original = deploy.mark_backup
        def mark(path, status):
            if path == paths[0]:
                raise PermissionError("private-path-not-printed")
            return original(path, status)
        with patch.object(deploy, "mark_backup", side_effect=mark), patch.object(deploy, "run_installer", return_value=0):
            self.assertEqual(deploy.deploy(ROOT, self.target, configure_only=True), 0)
        text = " ".join(self.output.getvalue().split())
        self.assertEqual(text.count("Резервные копии транзакций:"), 1)
        self.assertNotIn("private-path-not-printed", text)
        self.assertTrue(paths[0].exists())

