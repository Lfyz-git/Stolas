import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HAS_TOOLS = (ROOT / "tools/timezones.py").is_file()
if HAS_TOOLS:
    from tools import timezones, integrate, manage, install


@unittest.skipUnless(HAS_TOOLS, "Host presentation is outside Core image")
class TimezoneTests(unittest.TestCase):
    def test_moscow_and_day_boundary(self):
        self.assertIn("2026-10-10 23:18:00", timezones.display("2026-10-10T20:18:00Z", "Europe/Moscow"))
        self.assertIn("23:18:23 МСК +0300", timezones.display("2026-10-10T20:18:23Z", "Europe/Moscow"))
        self.assertIn("2026-10-11 01:18:00", timezones.display("2026-10-10T22:18:00+00:00", "Europe/Moscow"))

    def test_berlin_dst_forward_and_repeated_hour(self):
        for utc, local in (("2026-03-29T00:30:00Z", "01:30:00 CET +0100"), ("2026-03-29T01:30:00Z", "03:30:00 CEST +0200"), ("2026-10-25T00:30:00Z", "02:30:00 CEST +0200"), ("2026-10-25T01:30:00Z", "02:30:00 CET +0100")):
            self.assertIn(local, timezones.display(utc, "Europe/Berlin"))

    def test_copied_localtime_checks_metadata_and_ignores_process_tz(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "etc").mkdir()
            (root / "usr/share/zoneinfo/Europe").mkdir(parents=True)
            (root / "etc/timezone").write_text("Europe/Moscow")
            (root / "etc/localtime").write_bytes(b"zonefile")
            (root / "usr/share/zoneinfo/Europe/Moscow").write_bytes(b"zonefile")
            with patch.dict(os.environ, {"TZ": "Etc/UTC"}):
                self.assertEqual(timezones.discover(root)["name"], "Europe/Moscow")
            (root / "etc/localtime").write_bytes(b"other")
            self.assertIsNone(timezones.discover(root)["name"])
            self.assertIn("не соответствует", timezones.discover(root)["error"])

    @unittest.skipIf(os.name == "nt", "POSIX OS localtime symlink")
    def test_localtime_wins_over_stale_timezone_and_changes_are_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "etc").mkdir()
            zones = root / "usr/share/zoneinfo/Europe"
            zones.mkdir(parents=True)
            for name in ("Berlin", "Moscow"):
                (zones / name).touch()
            (root / "etc/timezone").write_text("Etc/UTC")
            local = root / "etc/localtime"
            for name in ("Berlin", "Moscow"):
                local.unlink(missing_ok=True)
                local.symlink_to(zones / name)
                self.assertEqual(timezones.discover(root)["name"], "Europe/" + name)

    def test_unknown_zone_never_defaults_to_utc_and_alias_errors_are_specific(self):
        with tempfile.TemporaryDirectory() as folder:
            info = timezones.discover(Path(folder))
            self.assertIsNone(info["name"])
            with patch.object(timezones, "discover", return_value=info), self.assertRaisesRegex(ValueError, "Не удалось определить"):
                timezones.require()
        for name in ("+3", "MSK"):
            with self.assertRaisesRegex(ValueError, "IANA timezone"):
                timezones.validate(name)
        with patch.object(timezones, "ZoneInfo", side_effect=timezones.ZoneInfoNotFoundError), self.assertRaisesRegex(ValueError, "база IANA"):
            timezones.validate("Europe/Moscow")

    def test_human_history_preserves_json_and_renders_in_host_zone(self):
        row = {"time": "2026-10-10T20:18:00Z", "node": "test", "status": "ok", "id": "cycle"}
        before = json.dumps([row])
        with tempfile.TemporaryDirectory() as folder:
            output = io.StringIO()
            with patch("sys.argv", ["stolas", "--root", folder, "history", "--human"]), patch.object(manage.resources, "command", return_value=["docker"]), patch.object(manage.resources, "compose", return_value=["docker", "compose"]), patch.object(timezones, "discover", return_value={"name": "Europe/Moscow"}), patch.object(manage.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, before)) as command, contextlib.redirect_stdout(output):
                self.assertEqual(manage.main(), 0)
            self.assertIn("23:18:00", output.getvalue())
            self.assertIn("Europe/Moscow", output.getvalue())
            self.assertEqual(command.call_args.args[0][-1], "history")
        self.assertEqual(json.dumps([row]), before)

    def test_run_time_uses_same_host_zone_and_keeps_result_utc(self):
        row = {"time": "2026-10-10T20:18:00Z", "status": "ok"}
        output = io.StringIO()
        with patch.object(install, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(row))), patch.object(timezones, "discover", return_value={"name": "Europe/Moscow"}), contextlib.redirect_stdout(output):
            self.assertEqual(install.first_test(["docker", "compose"], ROOT), row)
        self.assertIn("23:18:00", output.getvalue())
