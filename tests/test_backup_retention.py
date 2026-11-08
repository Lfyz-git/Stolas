"""Synthetic transaction snapshots; no daemon or production state is used."""
import contextlib
import io
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

    def test_retention_boundary_and_idempotence(self):
        for count in (0, 3, 4):
            with self.subTest(count=count):
                for path in self.backups.iterdir():
                    shutil.rmtree(path)
                paths = [self.snapshot(number) for number in range(count)]
                self.prune()
                self.assertEqual(self.names(), {path.name for path in paths[-3:]})
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
        self.assertIn("Не удалось проверить точки.", " ".join(self.output.getvalue().split()))

    def test_missing_snapshot_and_invalid_metadata_are_not_deleted(self):
        paths = [self.snapshot(number) for number in range(6)]
        (paths[0] / "files/compose.yaml").unlink()
        deploy.atomic_json(paths[1] / "state.json", {"files": {}})
        state = deploy.read_json(paths[2] / "state.json")
        state["created_ns"] = "invalid"
        deploy.atomic_json(paths[2] / "state.json", state)
        self.prune()
        self.assertEqual(len(self.names()), 6)

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
