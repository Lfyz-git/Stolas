"""Owned PATH wrappers; never overwrite a command or edit shell profiles."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from tools import diagnostics


def state_path(root):
    from tools import layout
    return layout.bounded(root, ".stolas/state/entrypoint.json")


def read(root):
    path = state_path(root)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def write_state(root, record):
    from tools.deploy import atomic_json
    path = state_path(root)
    atomic_json(path, record)
    # atomic_json preserves the installation owner, including after sudo.


def content(root):
    target = shlex.quote(str(Path(root).absolute() / "stolas"))
    message = shlex.quote("Установка Stolas не найдена: " + str(root) + ". Повторите установку.")
    return ("#!/bin/sh\n# Stolas managed command\nif [ ! -x " + target + " ]; then\n"
            "    printf '%s\\n' " + message + " >&2\n    exit 127\nfi\nexec " + target + ' "$@"\n').encode("utf-8")


def owned(root, record):
    path = Path(record.get("path", ""))
    expected = content(root)
    return (path.is_absolute() and not path.is_symlink() and path.is_file()
            and record.get("root") == str(Path(root).absolute())
            and record.get("sha256") == hashlib.sha256(expected).hexdigest()
            and path.read_bytes() == expected)


def in_path(directory):
    return Path(directory).resolve() in {Path(p or os.curdir).resolve() for p in os.environ.get("PATH", "").split(os.pathsep)}


def install(root, directory=None, name="stolas"):
    from tools import layout
    root = Path(root).absolute()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", name):
        raise ValueError("Имя команды: 1–63 латинских букв, цифр, _ или -")
    manifest = root / ".stolas/state/managed.json"
    files = json.loads(manifest.read_text(encoding="utf-8")).get("files", []) if manifest.is_file() else []
    required = [p for p in files if p == "stolas" or p.startswith(".stolas/installer/")]
    if not layout.runtime(root) or not required or not (root / "stolas").is_file() or any(not layout.bounded(root, p).is_file() for p in required):
        raise RuntimeError("Установка не завершена. Повторите установку перед добавлением команды")
    directory = Path(directory or Path.home() / ".local/bin").absolute()
    # Canonicalize the parent, never the command: a foreign symlink is a conflict.
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    destination = directory / name
    previous = read(root)
    if previous.get("path"):
        if previous["path"] == str(destination) and owned(root, previous):
            return previous
        raise RuntimeError("Команда уже настроена или изменена. Сначала выполните " + diagnostics.command(root, "command remove"))
    existing = shutil.which(name)
    if destination.exists() or destination.is_symlink() or existing and Path(existing).absolute() != root / "stolas":
        raise FileExistsError("Имя «" + name + "» занято. Выберите другое имя команды")
    value = content(root)
    record = {"root": str(root), "path": str(destination), "sha256": hashlib.sha256(value).hexdigest()}
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o755)
    created = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(value)
            file.flush()
            os.chmod(destination, 0o755)
            os.fsync(file.fileno())
        write_state(root, record)
    except BaseException:
        actual = destination.lstat() if destination.exists() else None
        if actual and not destination.is_symlink() and (actual.st_dev, actual.st_ino) == (created.st_dev, created.st_ino):
            destination.unlink()
        raise
    return record


def check_removal(root):
    record = read(root)
    if record.get("path") and owned(root, record) and not os.access(Path(record["path"]).parent, os.W_OK | os.X_OK):
        raise RuntimeError("Нет прав на удаление команды " + record["path"] + ". Повторите удаление с sudo и абсолютным путём к " + str(Path(root) / "stolas"))


def remove(root):
    from tools.terminal import ui
    check_removal(root)
    record = read(root)
    if record.get("path"):
        path = Path(record["path"])
        if owned(root, record):
            path.unlink()
            ui().result("Команда удалена: " + str(path))
        elif path.exists() or path.is_symlink():
            ui().result("Команда изменена и сохранена: " + str(path), "warning")
    state_path(root).unlink(missing_ok=True)


def configure(root, scope=None, name=None):
    from tools.install import ask, choice, LABELS
    from tools.terminal import ui
    terminal = ui()
    terminal.stage("Команда из любого каталога")
    previous = read(root)
    if previous.get("path"):
        terminal.result("Команда настроена: " + previous["path"] if owned(root, previous)
                        else "Команда изменилась. Проверьте " + diagnostics.command(root, "command status"), "success" if owned(root, previous) else "warning")
        return
    user_directory = Path.home() / ".local/bin"
    if "SUDO_USER" in os.environ:
        terminal.line("При sudo используются HOME и PATH этой сессии. Проверьте каталог команды.")
    LABELS.update(user="Для этой учётной записи (~/.local/bin)", system="Для всех пользователей (/usr/local/bin)")
    scope = scope or choice("Где разместить команду?", ("user", "system", "later"), "user" if in_path(user_directory) else "later")
    if scope == "later":
        write_state(root, {"declined": True})
        terminal.line("Позже: " + diagnostics.command(root, "command install"))
        return
    directory = user_directory if scope == "user" else Path("/usr/local/bin")
    terminal.line("Каталог: " + str(directory))
    if scope == "system":
        if choice("Установить общую команду в /usr/local/bin?", ("apply", "cancel"), "cancel") != "apply":
            return
        if not os.access(directory if directory.exists() else directory.parent, os.W_OK):
            raise PermissionError("Нет прав на /usr/local/bin. Выберите пользовательский каталог или запустите эту команду с sudo")
    candidate = name or "stolas"
    while True:
        try:
            record = install(root, directory, candidate)
            break
        except FileExistsError:
            terminal.result("Имя «" + candidate + "» занято; существующая команда сохранена.", "warning")
            candidate = ask("Другое имя команды (:cancel — оставить без команды)", "stolas-home")
    terminal.result("Команда установлена: " + record["path"])
    if not in_path(directory):
        terminal.line("Для текущей сессии добавьте каталог в PATH:")
        terminal.line("export PATH=" + shlex.quote(str(directory)) + ':"$PATH"')
        terminal.line("Для новых сессий добавьте эту строку в настройки своей оболочки.")


def offer(root):
    from tools.install import Back, Cancel, Rescan
    from tools.terminal import ui
    if read(root):
        return
    try:
        configure(root)
    except (Back, Cancel, Rescan, EOFError, KeyboardInterrupt):
        ui().line("Команда в PATH не настроена. Позже: " + diagnostics.command(root, "command install"))
    except (OSError, ValueError, RuntimeError) as error:
        from tools import layout
        ui().result((layout.os_error(error) if isinstance(error, OSError) else str(error)) + ". Core продолжает работать; повтор: " + diagnostics.command(root, "command install"), "warning")
