"""Transactional runtime installation. Backups contain exact files, never a broad delete."""
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
sys.path.insert(0, str(ROOT))
from tools import layout

PRIVATE = {".env", "config/local.json", "n8n/local.json", "n8n/install-state.json", "n8n/settings.json"}
STATE = {".stolas-draft.json": "draft.json", ".stolas-progress.json": "progress.json", ".stolas-install-status.json": "status.json"}


def safe_path(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Символические ссылки в пути установки запрещены")
    if path == Path(path.anchor) or path == Path.home() or str(path) in ("/opt", "/etc", "/usr", "/home", "/var", "/tmp"):
        raise ValueError("Укажите отдельный каталог, например /opt/stolas")
    return path


def atomic_json(path, data):
    layout.private_dir(path.parent)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as file:
            os.chmod(temporary, 0o600)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def install_lock(target):
    target = safe_path(target)
    target.mkdir(parents=True, exist_ok=True)
    layout.initialize(target)
    paths = [layout.bounded(target, ".stolas/state/install.lock")]
    if (target / ".stolas-install.lock").exists():
        paths.insert(0, layout.bounded(target, ".stolas-install.lock"))
    descriptors = []
    try:
        for path in paths:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            descriptors.append(fd)
            if os.name == "nt":
                import msvcrt
                os.write(fd, b"0")
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except (BlockingIOError, PermissionError):
        raise RuntimeError("Другой установщик уже работает с этим каталогом или нет прав на запись") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def installed(target):
    if layout.runtime(target):
        return (target / "stolas").is_file() and (target / "compose.yaml").is_file()
    return all((target / name).is_file() and not (target / name).is_symlink()
               for name in ("install.sh", "tools/install.py", "agent/config.py", "compose.yaml"))


def recoverable(target):
    metadata = {".stolas", ".stolas-install.lock", ".stolas-install-status.json", ".stolas-transaction.json", ".stolas-managed.json", ".stolas-backups", ".stolas-draft.json", ".stolas-progress.json"}
    return all(path.name in metadata or (path.is_dir() and not path.is_symlink() and
               all(item.is_dir() and not item.is_symlink() for item in path.rglob("*"))) for path in target.iterdir())


def inventory(source):
    return sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()
                  and not p.is_symlink() and not any(part in (".git", "__pycache__", "dist", ".stolas") for part in p.relative_to(source).parts))


def checked_file(root, name):
    if name in PRIVATE:
        raise ValueError("Некорректный путь управляемого файла")
    return layout.bounded(root, name)


def replace_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".stolas-tmp-" + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else (default if default is not None else {})


def runtime_files(source):
    if layout.runtime(source):
        manifest = read_json(layout.bounded(source, ".stolas/state/managed.json"))
        return {name: layout.bounded(source, name) for name in manifest.get("files", [])}
    result = {}
    for name in inventory(source):
        if name.startswith("agent/") and name.endswith(".py"):
            result[".stolas/build/" + name] = source / name
        if name in ("Dockerfile", ".dockerignore", "LICENSE", "config/example.json", "config/apk.lock.json", "config/apk-aarch64.lock", "config/apk-x86_64.lock", "tools/install-apk.sh"):
            result[".stolas/build/" + name] = source / name
        installer_names = {"tools/deploy.py", "tools/install.py", "tools/manage.py", "tools/resources.py", "tools/layout.py", "tools/environment.py", "tools/terminal.py", "tools/integrate.py", "tools/legacy-v0.4.0.json"}
        if name in installer_names or name in ("tools/install-docker.sh", "agent/config.py", "agent/__init__.py", "config/example.json", "n8n/stolas.json", "LICENSE"):
            result[".stolas/installer/" + name] = source / name
        if name in ("compose.yaml", "stolas"):
            result[name] = source / name
    return result


def legacy_files(target):
    manifest = read_json(layout.bounded(target, ".stolas-managed.json"))
    known = read_json(ROOT / "tools/legacy-v0.4.0.json")
    names = set(manifest.get("files", [])) & set(known)
    if not manifest:
        names = {name for name, digest in known.items() if layout.bounded(target, name).is_file()
                 and hashlib.sha256((target / name).read_bytes()).hexdigest() == digest}
    names |= {name for name in STATE if layout.bounded(target, name).is_file()}
    names |= {name for name in (".stolas-managed.json", ".stolas-transaction.json", ".stolas-install.lock") if layout.bounded(target, name).is_file()}
    base = layout.bounded(target, ".stolas-backups")
    if base.exists():
        for path in base.rglob("*"):
            layout.bounded(target, path.relative_to(target))
            if path.is_file():
                names.add(path.relative_to(target).as_posix())
    for base in (target, target / "config", target / "n8n"):
        if base.is_dir():
            for path in base.glob("*.bak-*"):
                if path.is_file():
                    names.add(path.relative_to(target).as_posix())
    names |= {name for name in PRIVATE if layout.bounded(target, name).is_file() and name.startswith("n8n/")}
    return names


def prune_empty(target, names):
    directories = set()
    for name in names:
        directories.update(p for p in (target / name).parents if p != target and target in p.parents)
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def stage_sources(source, target, ref, digest):
    layout.initialize(target)
    files = runtime_files(source)
    manifest_path = layout.bounded(target, ".stolas/state/managed.json")
    previous = read_json(manifest_path)
    legacy = legacy_files(target) if not layout.runtime(target) else set(previous.get("files", [])) - set(files)
    old_names = set(previous.get("files", [])) | legacy
    for name in files:
        path = layout.bounded(target, name)
        if path.exists() and name not in old_names and name != "compose.yaml":
            raise ValueError("Файл установки занят: " + name + ". Переместите его и повторите команду")
    backup = target / ".stolas/backups/transactions" / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    layout.private_dir(backup)
    state = {"files": {}, "modes": {}, "new_hashes": {}, "previous_manifest": previous, "legacy": sorted(legacy),
             "configured": (target / ".env").is_file() and (target / "config/local.json").is_file()}
    extras = {".env", "config/local.json", ".stolas/state/resources.json", ".stolas/state/handover.json", ".stolas/state/draft.json", ".stolas/state/progress.json", ".stolas/integrations/n8n/local.json", ".stolas/integrations/n8n/settings.json", ".stolas/integrations/n8n/install-state.json"}
    for name in sorted(set(files) | old_names | extras):
        path = layout.bounded(target, name)
        state["files"][name] = path.is_file()
        if path.is_file():
            state["modes"][name] = path.stat().st_mode & 0o777
            destination = backup / "files" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            os.chmod(destination, 0o600)
        if name in files:
            state["new_hashes"][name] = hashlib.sha256(files[name].read_bytes()).hexdigest()
    atomic_json(backup / "state.json", state)
    journal = target / ".stolas/state/transaction.json"
    atomic_json(journal, {"phase": "prepared", "backup": backup.relative_to(target).as_posix(), "configured": state["configured"]})
    try:
        for name, original in files.items():
            replace_file(original, layout.bounded(target, name))
        if (target / "stolas").is_file():
            os.chmod(target / "stolas", 0o755)
        for name, destination in STATE.items():
            old = layout.bounded(target, name)
            if old.is_file():
                replace_file(old, layout.bounded(target, ".stolas/state/" + destination))
                os.chmod(target / ".stolas/state" / destination, 0o600)
        for name in PRIVATE:
            old = layout.bounded(target, name)
            if name.startswith("n8n/") and old.is_file():
                destination = layout.integration_path(target, Path(name).name)
                layout.private_dir(destination.parent)
                replace_file(old, destination)
                os.chmod(destination, 0o600)
        atomic_json(manifest_path, {"files": sorted(files), "hashes": state["new_hashes"], "ref": ref, "archive_sha256": digest})
        for directory in (target / ".stolas").rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                os.chmod(directory, 0o700)
        atomic_json(journal, {"phase": "installed", "backup": backup.relative_to(target).as_posix(), "configured": state["configured"]})
    except BaseException:
        restore(target, backup)
        atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix()})
        raise
    return backup


def restore(target, backup):
    state = read_json(layout.bounded(target, backup.relative_to(target) / "state.json"))
    for name, existed in state["files"].items():
        path = layout.bounded(target, name)
        if existed:
            replace_file(layout.bounded(backup / "files", name), path)
            os.chmod(path, state.get("modes", {}).get(name, 0o644))
            if name == ".env" or name.startswith(".stolas/state/"):
                os.chmod(path, 0o600)
            elif name in ("stolas", "install.sh"):
                os.chmod(path, 0o755)
        elif path.is_file():
            expected = state["new_hashes"].get(name)
            if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError("Управляемый файл изменён после обновления; сохраните его и повторите откат: " + name)
            path.unlink()
    atomic_json(target / ".stolas/state/managed.json", state["previous_manifest"])
    prune_empty(target, state["files"])


def finalize(target, backup):
    state = read_json(backup / "state.json")
    new_names = set(state["new_hashes"])
    for name in state["legacy"]:
        if name in new_names:
            continue
        path = layout.bounded(target, name)
        original = layout.bounded(backup / "files", name)
        if path.is_file() and original.is_file() and path.read_bytes() == original.read_bytes():
            try:
                path.unlink()
            except PermissionError:
                if name != ".stolas-install.lock" or os.name != "nt":
                    raise
    prune_empty(target, state["legacy"])


def run_installer(target, configure_only=False, reuse=False, recover=False):
    args = [sys.executable, str(layout.code_root(target) / "tools/install.py"), "--root", str(target)]
    if configure_only:
        args.append("--configure-only")
    if reuse:
        args.append("--reuse-config")
    if recover:
        args.append("--recover")
    return subprocess.run(args, cwd=target).returncode


def rollback_files(target, backup, configure_only=False):
    from tools import resources
    state = read_json(backup / "state.json")
    docker = resources.stop_replacement(target) if state.get("configured") and not configure_only else None
    restore(target, backup)
    if docker and not resources.load(target, "resources.json").get("uninstalled"):
        resources.restart(target, docker)


def deploy(source, target, ref="checkout", digest="", action=None, configure_only=False):
    target = safe_path(target)
    pending_path = layout.bounded(target, ".stolas/state/transaction.json")
    pending = read_json(pending_path)
    if target.exists() and any(target.iterdir()) and not installed(target) and not recoverable(target) and not pending.get("backup"):
        raise ValueError("В каталоге посторонние файлы. Выберите другой --dir; файлы не изменены")
    with install_lock(target):
        diagnostic = target / ".stolas/state/status.json"
        journal = target / ".stolas/state/transaction.json"
        pending = read_json(journal)
        if pending.get("phase") in ("prepared", "installed", "interrupted") and pending.get("configured"):
            backup = layout.bounded(target, pending["backup"])
            rollback_files(target, backup, configure_only)
            atomic_json(journal, {**pending, "phase": "rolled_back"})
            from tools.terminal import ui
            ui().result("Предыдущая установка восстановлена после прерывания")
        already = installed(target)
        configured = (target / ".env").is_file() and (target / "config/local.json").is_file()
        if already and not action:
            from tools.install import choice, LABELS
            from tools.terminal import ui
            LABELS.update(reconfigure="Изменить настройки", update="Обновить", rollback="Вернуть предыдущую версию")
            ui().stage("Управление установкой")
            ui().line("Каталог: " + str(target))
            action = choice("Что сделать?", ("reconfigure", "update", "rollback", "cancel"), "reconfigure")
        action = action or "reconfigure"
        if action == "cancel":
            return 0
        if action not in ("reconfigure", "update", "rollback"):
            raise ValueError("Неизвестное действие установки")
        backup = None
        try:
            atomic_json(diagnostic, {"stage": action, "ref": ref, "time": time.time()})
            if action == "rollback":
                previous = read_json(journal)
                if not previous.get("backup"):
                    raise RuntimeError("Нет резервной копии для отката")
                backup = layout.bounded(target, previous["backup"])
                if not read_json(backup / "state.json").get("configured"):
                    raise RuntimeError("Резервная копия не содержит предыдущую установленную версию")
                rollback_files(target, backup, configure_only)
                atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix()})
                from tools.terminal import ui
                ui().result("Предыдущая версия восстановлена; история сохранена")
                return 0
            if source != target or configured and layout.runtime(target):
                if source == target:
                    current = read_json(target / ".stolas/state/managed.json")
                    ref, digest = current.get("ref", ref), current.get("archive_sha256", digest)
                backup = stage_sources(source, target, ref, digest)
            code = run_installer(target, configure_only, reuse=configured and action == "update")
            if code in (1, 3) and backup and (configured or code == 3):
                rollback_files(target, backup, configure_only)
                atomic_json(diagnostic, {"stage": "rolled_back" if code == 1 else "cancelled", "exit_code": code})
            else:
                if backup and code in (0, 2):
                    finalize(target, backup)
                atomic_json(diagnostic, {"stage": "complete" if code == 0 else "interrupted" if code == 130 else "needs_attention", "exit_code": code, "ref": ref})
            if backup:
                atomic_json(journal, {"phase": "interrupted" if code == 130 or code == 1 and not configured else "rolled_back" if code in (1, 3) else "complete", "backup": backup.relative_to(target).as_posix(), "configured": configured})
            return code
        except BaseException:
            if backup:
                rollback_files(target, backup, configure_only)
                atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix(), "configured": configured})
            raise


def main():
    parser = argparse.ArgumentParser(description="Установка, обновление и откат Stolas")
    parser.add_argument("--target", type=Path, default=Path.home() / "stolas")
    parser.add_argument("--source-ref", default="checkout")
    parser.add_argument("--source-sha256", default="")
    parser.add_argument("--action", choices=("reconfigure", "update", "rollback", "cancel", "uninstall"))
    parser.add_argument("--configure-only", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "uninstall":
            from tools.manage import uninstall
            return uninstall(safe_path(args.target))
        if args.diagnose:
            from tools import environment
            print(json.dumps(environment.discover(safe_path(args.target)), ensure_ascii=False, indent=2))
            return 0
        return deploy(ROOT, args.target, args.source_ref, args.source_sha256, args.action, args.configure_only)
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt, EOFError) as error:
        from tools.terminal import ui
        ui().result(str(error) if not isinstance(error, (KeyboardInterrupt, EOFError)) else "Ввод прерван. Повторите команду для продолжения", "error")
        return 130 if isinstance(error, (KeyboardInterrupt, EOFError)) else 1


if __name__ == "__main__":
    sys.exit(main())
