"""Paths shared by the installer and the offline management command."""
import os
from pathlib import Path

VERSION = "0.5.0"


def runtime(root):
    return (Path(root) / ".stolas/installer/tools/install.py").is_file()


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


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def initialize(root):
    for name in (".stolas", ".stolas/state", ".stolas/backups", ".stolas/integrations"):
        private_dir(bounded(root, name))
