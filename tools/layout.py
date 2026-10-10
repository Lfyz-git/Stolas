"""Paths shared by the installer and the offline management command."""
import os
import errno
import json
from pathlib import Path

VERSION = "0.5.1"


def runtime(root):
    try:
        return (Path(root) / ".stolas/installer/tools/install.py").is_file()
    except OSError:
        return False


def code_root(root):
    return Path(root) / ".stolas/installer" if runtime(root) else Path(root)


def state_path(root, name):
    if runtime(root):
        return Path(root) / ".stolas/state" / name.removeprefix(".stolas-")
    return Path(root) / name


def integration_path(root, name):
    return Path(root) / (".stolas/integrations/n8n" if runtime(root) else "n8n") / name


def bounded(root, name):
    root = Path(os.path.abspath(root))
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Недопустимый путь файла установки")
    path = root / relative
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Символические ссылки в пути установки запрещены")
        if item == root:
            break
    return path


def installation_root(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.name == ".stolas":
            return parent.parent
        if (parent / ".stolas").is_dir():
            return parent
    return None


def owner_for(path):
    root = installation_root(path)
    candidate = root or (path if path.exists() else path.parent)
    return candidate.stat()


def keep_owner(path, owner=None):
    if hasattr(os, "chown") and os.geteuid() == 0:
        if path.is_file() and path.stat().st_nlink != 1:
            raise ValueError("Управляемый файл имеет hardlink; смена владельца запрещена: " + str(path))
        owner = owner or owner_for(path)
        os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)


def directory(path):
    """Only directories on an explicitly managed write path; never walk files."""
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Отказ записи через symlink")
    root = installation_root(path)
    missing = [p for p in (path, *path.parents) if not p.exists()]
    path.mkdir(parents=True, exist_ok=True)
    managed = [p for p in (path, *path.parents) if root and p != root and root in p.parents]
    for item in set(missing + managed):
        keep_owner(item)
        if root and (item == root / ".stolas" or root / ".stolas" in item.parents):
            os.chmod(item, 0o700)


def private_dir(path):
    directory(path)
    os.chmod(path, 0o700)


STATE_FILES = ("managed.json", "transaction.json", "resources.json", "handover.json", "identity.json", "draft.json", "progress.json", "status.json", "install.lock", "entrypoint.json", "error.json")


def access_report(root):
    """Read-only metadata/access checks. Contents and secrets never enter output."""
    root = Path(root).absolute()
    names = {".", ".stolas", ".stolas/state", ".stolas/backups", ".stolas/installer", ".stolas/build", ".stolas/integrations", "config", ".env", "config/local.json"}
    names.update(".stolas/state/" + name for name in STATE_FILES)
    try:
        manifest = root / ".stolas/state/managed.json"
        if os.access(manifest, os.R_OK):
            names.update(json.loads(manifest.read_text(encoding="utf-8")).get("files", []))
    except (OSError, ValueError):
        pass
    rows = []
    expected = root.stat().st_uid if root.exists() else None
    for name in sorted(names):
        path = root if name == "." else bounded(root, name)
        try:
            meta = path.stat()
        except FileNotFoundError:
            continue
        except PermissionError:
            rows.append({"path": str(path), "readable": False, "writable": False, "owner_mismatch": True})
            continue
        rows.append({"path": str(path), "uid": meta.st_uid, "gid": meta.st_gid, "mode": oct(meta.st_mode & 0o777),
                     "readable": os.access(path, os.R_OK | (os.X_OK if path.is_dir() else 0)),
                     "writable": os.access(path if path.is_dir() else path.parent, os.W_OK | os.X_OK),
                     "owner_mismatch": meta.st_uid != expected})
    return {"owner_uid": expected, "current_uid": getattr(os, "geteuid", lambda: None)(), "paths": rows}


def preflight(root):
    for row in access_report(root)["paths"]:
        if not row["readable"] or not row["writable"]:
            raise PermissionError(errno.EACCES, "Нет прав на чтение/запись", row["path"])


def os_error(error):
    reasons = {errno.EACCES: "Нет прав на чтение/запись", errno.EPERM: "Операция не разрешена", errno.EROFS: "Файловая система только для чтения", errno.ENOENT: "Файл или каталог не найден"}
    message = reasons.get(error.errno, type(error).__name__ + " (errno=" + str(error.errno) + ")")
    return message + (": " + str(error.filename) if error.filename else "")


def initialize(root):
    for name in (".stolas", ".stolas/state", ".stolas/backups", ".stolas/integrations"):
        private_dir(bounded(root, name))
