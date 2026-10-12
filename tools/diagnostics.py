"""Safe user errors: paths/errno and stack locations, never exception payloads."""
import json
from pathlib import Path
import shlex
import traceback
from tools import layout


def command(root, action="diagnose"):
    try:
        from tools import entrypoints
        record = entrypoints.read(root)
        if entrypoints.owned(root, record) and entrypoints.in_path(Path(record["path"]).parent):
            return shlex.quote(Path(record["path"]).name) + " " + action
    except (OSError, ValueError):
        pass
    return shlex.quote(str(Path(root).absolute() / "stolas")) + " " + action


def report(root, error):
    from tools.terminal import ui
    message = layout.os_error(error) if isinstance(error, OSError) else "Действие не завершено: " + type(error).__name__
    ui().result(message, "error")
    ui().line("Диагностика: " + command(root))
    if isinstance(error, PermissionError):
        ui().line("Проверьте владельца указанных управляемых файлов. Для root-owned установки используйте sudo с абсолютным путём к stolas.")
    else:
        from tools.deploy import atomic_json
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
        try:
            atomic_json(layout.bounded(root, ".stolas/state/error.json"), {"exception": type(error).__name__, "errno": getattr(error, "errno", None), "frames": frames})
        except (OSError, ValueError):
            pass


def permissions(facts):
    from tools.terminal import ui
    for row in facts.get("permissions", {}).get("paths", []):
        if not row["readable"] or not row["writable"] or row["owner_mismatch"]:
            ui().result("Права: " + row["path"] + "; uid=" + str(row.get("uid", "?")) + "; mode=" + row.get("mode", "?") + "; чтение=" + str(row["readable"]) + "; запись=" + str(row["writable"]), "warning")
