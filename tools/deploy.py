"""Locked source installation/update. Never recursively delete a user directory."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {".env", "config/local.json", "n8n/local.json", "n8n/install-state.json", "n8n/settings.json"}


def safe_path(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Символические ссылки в пути установки запрещены")
    if path == Path(path.anchor) or path == Path.home() or str(path) in ("/opt", "/etc", "/usr", "/home", "/var", "/tmp"):
        raise ValueError("Укажите отдельный каталог, например /opt/stolas")
    return path


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as file:
            os.chmod(temporary, 0o600)
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def install_lock(target):
    target = safe_path(target)
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise ValueError("Путь установки не является каталогом")
    path = target / ".stolas-install.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            os.write(fd, b"0")
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise RuntimeError("Другой установщик уже работает с этим каталогом") from None
    finally:
        os.close(fd)


def installed(target):
    return all((target / name).is_file() and not (target / name).is_symlink()
               for name in ("install.sh", "tools/install.py", "agent/config.py", "compose.yaml"))


def recoverable(target):
    metadata = {".stolas-install.lock", ".stolas-install-status.json", ".stolas-transaction.json", ".stolas-managed.json", ".stolas-backups"}
    if all(path.name in metadata for path in target.iterdir()):
        return True
    journal = checked_file(target, ".stolas-transaction.json")
    if journal.is_file():
        state = json.loads(journal.read_text())
        backup = checked_file(target, state.get("backup", ""))
        return state.get("phase") in ("prepared", "installed") and (backup / "state.json").is_file()
    return False


def inventory(source):
    files = []
    for path in source.rglob("*"):
        name = path.relative_to(source).as_posix()
        if any(part.startswith(".stolas-") or part in (".git", "data", "__pycache__", ".venv", ".docker-client") for part in path.relative_to(source).parts):
            continue
        if name in PRIVATE or ".bak-" in path.name or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Symlink в исходном архиве")
        if path.is_file():
            files.append(name)
    return sorted(files)


def checked_file(root, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or name in PRIVATE or not relative.parts:
        raise ValueError("Некорректный путь управляемого файла")
    path = root / relative
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise ValueError("Symlink в управляемом пути")
    return path


def replace_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".stolas-tmp-" + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def restore(target, backup):
    state = json.loads((backup / "state.json").read_text(encoding="utf-8"))
    for name, existed in state["files"].items():
        path = checked_file(target, name)
        if existed:
            replace_file(backup / "files" / name, path)
        elif path.is_file():
            # Delete only an exact file installed by this transaction.
            expected = state["new_hashes"].get(name)
            if expected and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
                path.unlink()
            else:
                raise RuntimeError("Новый файл изменён после обновления; автоматический откат остановлен")
    atomic_json(target / ".stolas-managed.json", state["previous_manifest"])


def stage_sources(source, target, ref, digest):
    names = inventory(source)
    manifest_path = target / ".stolas-managed.json"
    legacy_roots = {"README.md", "VALIDATION.md", "LICENSE", "Dockerfile", "compose.yaml", "bootstrap.sh", "install.sh", "requirements.txt", ".env.example", ".dockerignore", ".gitattributes", ".gitignore"}
    legacy_dirs = {"agent", "tools", "tests", "config", "n8n", ".github"}
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"files": [name for name in inventory(target) if name in legacy_roots or Path(name).parts[0] in legacy_dirs]}
    old_names = set(previous.get("files", []))
    # Refuse collisions with local files which were never part of Stolas.
    for name in names:
        path = checked_file(target, name)
        if path.exists() and name not in old_names and installed(target):
            raise ValueError("Новый исходный файл конфликтует с локальным файлом: " + name)
    checked_file(target, ".stolas-backups")
    backup = target / ".stolas-backups" / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    state = {"files": {}, "new_hashes": {}, "previous_manifest": previous}
    for name in sorted(set(names) | old_names):
        path = checked_file(target, name)
        state["files"][name] = path.is_file()
        if path.is_file():
            copy = backup / "files" / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
        if name in names:
            state["new_hashes"][name] = hashlib.sha256((source / name).read_bytes()).hexdigest()
    atomic_json(backup / "state.json", state)
    journal = target / ".stolas-transaction.json"
    atomic_json(journal, {"phase": "prepared", "backup": backup.relative_to(target).as_posix()})
    try:
        for name in names:
            replace_file(source / name, checked_file(target, name))
        # Removed upstream files are retained, never silently delete local code.
        atomic_json(manifest_path, {"files": sorted(set(names) | old_names), "ref": ref, "archive_sha256": digest})
        atomic_json(journal, {"phase": "installed", "backup": backup.relative_to(target).as_posix()})
    except BaseException:
        restore(target, backup)
        atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix()})
        raise
    return backup


def run_installer(target, configure_only=False, reuse=False):
    args = [sys.executable, str(target / "tools/install.py")]
    if configure_only:
        args.append("--configure-only")
    if reuse:
        args.append("--reuse-config")
    return subprocess.run(args, cwd=target).returncode


def deploy(source, target, ref="checkout", digest="", action=None, configure_only=False):
    target = safe_path(target)
    if target.exists() and any(target.iterdir()) and not installed(target) and not recoverable(target):
        raise ValueError("В каталоге посторонние файлы. Выберите другой --dir; файлы не изменены")
    with install_lock(target):
        if not installed(target) and not recoverable(target):
            raise ValueError("Каталог изменён другим процессом; установка остановлена")
        diagnostic = target / ".stolas-install-status.json"
        journal = target / ".stolas-transaction.json"
        if journal.exists():
            state = json.loads(journal.read_text())
            if state.get("phase") in ("prepared", "installed"):
                backup = checked_file(target, state["backup"])
                restore(target, backup)
                atomic_json(journal, {**state, "phase": "rolled_back"})
                print("Прерванное обновление восстановлено. Повторите запуск.")
                return 1
        already = installed(target)
        if already and source != target and action is None:
            print("Stolas уже установлен. reconfigure — повторная настройка; update — безопасное обновление; rollback — откат; cancel — выход.")
            action = input("Действие [reconfigure]: ").strip() or "reconfigure"
        action = action or "reconfigure"
        if action not in ("reconfigure", "update", "rollback", "cancel"):
            raise ValueError("Неизвестное действие установки")
        if action == "cancel":
            return 0
        backup = None
        try:
            atomic_json(diagnostic, {"stage": action, "ref": ref, "time": time.time()})
            if action == "rollback":
                checked_file(target, ".stolas-backups")
                backups = sorted(path for path in (target / ".stolas-backups").glob("*/state.json") if "install.sh" in json.loads(path.read_text())["previous_manifest"].get("files", []))
                if not backups:
                    raise RuntimeError("Нет резервной копии для отката")
                restore(target, backups[-1].parent)
            elif source != target and (not already or action == "update"):
                backup = stage_sources(source, target, ref, digest)
            code = run_installer(target, configure_only, reuse=already and action in ("update", "rollback"))
            if code == 1 and backup and already:
                restore(target, backup)
                recovery = run_installer(target, configure_only, reuse=True)
                atomic_json(diagnostic, {"stage": "rolled_back", "exit_code": code, "recovery_exit_code": recovery})
            else:
                atomic_json(diagnostic, {"stage": "complete" if code == 0 else "needs_attention", "exit_code": code, "ref": ref})
            if journal.exists():
                state = json.loads(journal.read_text())
                atomic_json(journal, {**state, "phase": "complete" if code != 1 else "failed"})
            return code
        except BaseException as error:
            if backup and already:
                restore(target, backup)
                recovery = run_installer(target, configure_only, reuse=True)
                atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix()})
                atomic_json(diagnostic, {"stage": "rolled_back", "error_type": type(error).__name__, "recovery_exit_code": recovery})
            else:
                atomic_json(diagnostic, {"stage": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed", "error_type": type(error).__name__})
            raise


def main():
    parser = argparse.ArgumentParser(description="Установка, обновление и откат Stolas")
    parser.add_argument("--target", type=Path, default=ROOT)
    parser.add_argument("--source-ref", default="checkout")
    parser.add_argument("--source-sha256", default="")
    parser.add_argument("--action", choices=("reconfigure", "update", "rollback", "cancel"))
    parser.add_argument("--configure-only", action="store_true")
    args = parser.parse_args()
    try:
        return deploy(ROOT, args.target, args.source_ref, args.source_sha256, args.action, args.configure_only)
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt, EOFError) as error:
        print("Stolas:", str(error) if not isinstance(error, (KeyboardInterrupt, EOFError)) else "Ввод прерван", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
