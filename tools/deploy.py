"""Transactional runtime installation. Backups contain exact files, never a broad delete."""
import argparse
import calendar
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import layout

PRIVATE = {".env", "config/local.json", "n8n/local.json", "n8n/install-state.json", "n8n/settings.json"}
STATE = {".stolas-draft.json": "draft.json", ".stolas-progress.json": "progress.json", ".stolas-install-status.json": "status.json"}
BACKUP_ROOT = ".stolas/backups/transactions"
BACKUP_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
BACKUP_LIMIT = 3
BACKUP_REPORT = ContextVar("backup_report", default=None)


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
        layout.keep_owner(temporary, layout.owner_for(path))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def install_lock(target):
    target = safe_path(target)
    target.mkdir(parents=True, exist_ok=True)
    layout.preflight(target)
    layout.initialize(target)
    paths = [layout.bounded(target, ".stolas/state/install.lock")]
    if (target / ".stolas-install.lock").exists():
        paths.insert(0, layout.bounded(target, ".stolas-install.lock"))
    descriptors = []
    try:
        for path in paths:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            descriptors.append(fd)
            layout.keep_owner(path)
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
    layout.directory(destination.parent)
    temporary = destination.with_name(destination.name + ".stolas-tmp-" + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        layout.keep_owner(temporary, layout.owner_for(destination))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else (default if default is not None else {})


def backup_warning(reason, key="operation"):
    # Fixed reason labels only: exception strings can contain private paths/data.
    report = BACKUP_REPORT.get()
    if report is not None:
        report["reasons"][(key, reason)] = reason


def old_restore_points(count):
    if count % 10 == 1 and count % 100 != 11:
        return f"Осталась {count} старая точка восстановления."
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"Остались {count} старые точки восстановления."
    return f"Остались {count} старых точек восстановления."


def restore_point_line(terminal, text, kind=None):
    for line in textwrap.wrap(text, width=terminal.width - 2):
        terminal.line("  " + line, kind)


@contextmanager
def backup_reporting(target):
    if BACKUP_REPORT.get() is not None:
        yield
        return
    report = {"reasons": {}, "checked": False, "pruning": False, "deleted": 0}
    token = BACKUP_REPORT.set(report)
    try:
        yield
    finally:
        try:
            if report["checked"] or report["reasons"]:
                records, protected, phase = inspect_backups(target)
                normal = sum(state is not None and state.get("retention_status") in ("complete", "rolled_back")
                             and not (path == protected and phase in ("prepared", "installed", "interrupted"))
                             for path, state in records)
                pending = sum(state is not None and (state.get("retention_status") == "pending" or
                              path == protected and phase in ("prepared", "installed", "interrupted")) for path, state in records)
                unchecked = sum(state is None for _, state in records)
                old = len(records) - normal - pending - unchecked
                from tools.terminal import ui
                terminal = ui()
                terminal.line()
                terminal.line("Точки восстановления")
                if normal:
                    restore_point_line(terminal, f"Сохранено: {normal}")
                if report["deleted"]:
                    restore_point_line(terminal, f"Удалено устаревших: {report['deleted']}")
                if old:
                    restore_point_line(terminal, old_restore_points(old), "warning")
                    restore_point_line(terminal, "Если они больше не нужны,")
                    restore_point_line(terminal, "их можно удалить вручную")
                    restore_point_line(terminal, "после проверки.")
                if pending:
                    restore_point_line(terminal, f"Незавершённых точек: {pending}", "warning")
                    restore_point_line(terminal, "Не удаляйте их до завершения")
                    restore_point_line(terminal, "восстановления установки.")
                if unchecked:
                    restore_point_line(terminal, f"Не удалось проверить: {unchecked}", "warning")
                    restore_point_line(terminal, "Проверьте сохранённые файлы")
                    restore_point_line(terminal, "и права доступа к ним.")
                reasons = set(report["reasons"].values())
                if "ошибка доступа или структуры при удалении" in reasons:
                    restore_point_line(terminal, "Часть устаревших точек")
                    restore_point_line(terminal, "не удалось удалить.", "warning")
                    restore_point_line(terminal, "Проверьте права доступа")
                    restore_point_line(terminal, "и сохранённые файлы.")
                elif "платформа не поддерживает безопасное удаление" in reasons:
                    restore_point_line(terminal, "Автоматическая очистка")
                    restore_point_line(terminal, "недоступна в этой системе.", "warning")
                elif not (old or unchecked) and reasons & {"не удалось записать подтверждённый статус", "повреждён сохранённый журнал", "некорректное свидетельство завершения", "предыдущий журнал нельзя использовать как свидетельство"}:
                    restore_point_line(terminal, "Сведения о сохранённых точках")
                    restore_point_line(terminal, "требуют ручной проверки.", "warning")
                if not records and not report["deleted"]:
                    restore_point_line(terminal, "Сохранённых точек нет.")
        except (OSError, ValueError, TypeError, AttributeError):
            from tools.terminal import ui
            terminal = ui()
            terminal.line()
            terminal.line("Точки восстановления")
            if report["deleted"]:
                restore_point_line(terminal, f"Удалено устаревших: {report['deleted']}")
            restore_point_line(terminal, "Не удалось проверить точки.", "warning")
            restore_point_line(terminal, "Проверьте сохранённые файлы,")
            restore_point_line(terminal, "ссылки и права доступа.")
        finally:
            BACKUP_REPORT.reset(token)


def backup_state(target, backup):
    """Validate the entire owned snapshot before considering recursive deletion."""
    root = layout.bounded(target, BACKUP_ROOT)
    if root.resolve() != root.absolute() or not backup.is_dir():
        raise ValueError("Invalid backup directory")
    if backup.parent != root or not BACKUP_NAME.fullmatch(backup.name):
        raise ValueError("Invalid transaction backup")
    layout.bounded(target, backup.relative_to(target))
    for directory, directories, files in os.walk(backup, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in directories + files)]:
            info = path.lstat()
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Linked backup")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("Special backup file")
    state = read_json(layout.bounded(backup, "state.json"))
    if not isinstance(state, dict) or not all(isinstance(state.get(key), dict) for key in ("files", "modes", "new_hashes", "previous_manifest")) or not isinstance(state.get("legacy"), list):
        raise ValueError("Incomplete backup state")
    for name, existed in state["files"].items():
        if not isinstance(name, str) or type(existed) is not bool:
            raise ValueError("Invalid snapshot inventory")
        layout.bounded(target, name)
        saved = layout.bounded(backup / "files", name)
        if existed and not saved.is_file():
            raise ValueError("Missing snapshot file")
    return state


def backup_order(backup, state):
    # Older releases have no sequence: their UTC directory name is stable even
    # when files are restored/touched. The name also breaks timestamp ties.
    sequence = state.get("created_ns")
    if sequence is None:
        sequence = calendar.timegm(time.strptime(backup.name[:16], "%Y%m%dT%H%M%SZ")) * 1_000_000_000
    if type(sequence) is not int or sequence < 0:
        raise ValueError("Invalid backup sequence")
    return sequence, backup.name


def mark_backup(backup, status):
    state = read_json(backup / "state.json")
    atomic_json(backup / "state.json", {**state, "retention_status": status})


def journal_backup(target, journal):
    if not isinstance(journal, dict):
        raise ValueError("Invalid journal")
    phase = journal.get("phase")
    if not journal:
        return None, None
    if phase not in ("prepared", "installed", "interrupted", "complete", "rolled_back"):
        raise ValueError("Invalid transaction phase")
    name = journal.get("backup")
    if not isinstance(name, str):
        raise ValueError("Missing recovery reference")
    backup = layout.bounded(target, name)
    if backup.parent != layout.bounded(target, BACKUP_ROOT) or not BACKUP_NAME.fullmatch(backup.name):
        raise ValueError("Invalid rollback reference")
    return backup, phase


def inspect_backups(target):
    """Validate the root/reference first; invalid references abort all pruning."""
    root = layout.bounded(target, BACKUP_ROOT)
    if root.resolve() != root.absolute():
        raise ValueError("Linked backup root")
    journal = read_json(layout.bounded(target, ".stolas/state/transaction.json"))
    protected, phase = journal_backup(target, journal)
    if protected is not None:
        backup_state(target, protected)
    records = []
    if root.exists():
        for backup in sorted(root.iterdir()):
            try:
                state = backup_state(target, backup)
                backup_order(backup, state)
            except (OSError, ValueError, TypeError):
                state = None
            records.append((backup, state))
    return records, protected, phase


def reconcile_legacy_backups(target, records, protected, phase):
    """Only terminal journals are positive completion evidence, never age/config.

    Later snapshots preserve a small previous_transaction receipt. Older snapshots
    may contain a saved journal in their validated file inventory. Conflicting or
    incomplete evidence always keeps a legacy snapshot outside the deletion pool.
    """
    evidence = {}
    if protected is not None:
        evidence.setdefault(protected, set()).add(phase)
    for path, state in records:
        if state is None:
            continue
        journals = []
        if "previous_transaction" in state:
            journals.append(state["previous_transaction"])
        for name in (".stolas/state/transaction.json", ".stolas-transaction.json"):
            if state["files"].get(name) is True:
                try:
                    journals.append(read_json(layout.bounded(path / "files", name)))
                except (OSError, ValueError, TypeError):
                    backup_warning("повреждён сохранённый журнал", path)
        for journal in journals:
            try:
                reference, status = journal_backup(target, journal)
                if reference is not None:
                    evidence.setdefault(reference, set()).add(status)
            except (OSError, ValueError, TypeError):
                backup_warning("некорректное свидетельство завершения", path)
    for path, state in records:
        if state is None or state.get("retention_status") is not None:
            continue
        phases = evidence.get(path, set())
        if phases and phases <= {"complete", "rolled_back"}:
            try:
                status = "rolled_back" if "rolled_back" in phases else "complete"
                mark_backup(path, status)
                state["retention_status"] = status
            except (OSError, ValueError, TypeError):
                backup_warning("не удалось записать подтверждённый статус", path)


def retain_transaction_backups(target):
    """Under install_lock, prune only after successful finalization and commit."""
    with backup_reporting(target):
        BACKUP_REPORT.get()["checked"] = True
        BACKUP_REPORT.get()["pruning"] = True
        try:
            records, protected, phase = inspect_backups(target)
            reconcile_legacy_backups(target, records, protected, phase)
            candidates = []
            for backup, state in records:
                if state is None:
                    backup_warning("неизвестная запись или повреждённая/небезопасная структура", backup)
                    continue
                status = state.get("retention_status")
                if status == "pending" or backup == protected and phase in ("prepared", "installed", "interrupted"):
                    backup_warning("незавершённая транзакция защищена", backup)
                    continue
                if status not in ("complete", "rolled_back"):
                    backup_warning("нет непротиворечивого подтверждения завершения" if status is None else "неизвестный статус транзакции", backup)
                    continue
                candidates.append((backup_order(backup, state), backup))
            candidates.sort(reverse=True)
            keep = {protected} if any(path == protected for _, path in candidates) else set()
            for _, path in candidates:
                if len(keep) >= BACKUP_LIMIT:
                    break
                keep.add(path)
            for _, backup in candidates:
                if backup in keep or backup == protected:
                    continue
                if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
                    backup_warning("платформа не поддерживает безопасное удаление", "platform")
                    break
                try:
                    backup_state(target, backup)
                    shutil.rmtree(backup)
                    report = BACKUP_REPORT.get()
                    if not backup.exists():
                        report["deleted"] += 1
                    else:
                        backup_warning("ошибка доступа или структуры при удалении", backup)
                except (OSError, ValueError, TypeError):
                    backup_warning("ошибка доступа или структуры при удалении", backup)
        except (OSError, ValueError, TypeError, AttributeError):
            backup_warning("некорректный корень, журнал или защищённая копия", "root")


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
        installer_names = {"tools/deploy.py", "tools/install.py", "tools/manage.py", "tools/resources.py", "tools/layout.py", "tools/environment.py", "tools/terminal.py", "tools/progress.py", "tools/integrate.py", "tools/entrypoints.py", "tools/diagnostics.py", "tools/timezones.py", "tools/legacy-v0.4.0.json"}
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
    layout.preflight(target)
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
    backup_root = layout.bounded(target, BACKUP_ROOT)
    created_ns = time.time_ns()
    if backup_root.exists():
        for previous_backup in backup_root.iterdir():
            try:
                previous_state = backup_state(target, previous_backup)
                created_ns = max(created_ns, backup_order(previous_backup, previous_state)[0] + 1)
            except (OSError, ValueError, TypeError):
                continue
    backup = backup_root / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    layout.private_dir(backup)
    state = {"files": {}, "modes": {}, "new_hashes": {}, "previous_manifest": previous, "legacy": sorted(legacy),
             "configured": (target / ".env").is_file() and (target / "config/local.json").is_file(),
             "created_ns": created_ns, "retention_status": "pending"}
    prior_journal = read_json(layout.bounded(target, ".stolas/state/transaction.json"))
    if prior_journal:
        try:
            journal_backup(target, prior_journal)
            state["previous_transaction"] = {key: prior_journal[key] for key in ("phase", "backup")}
        except (OSError, ValueError, TypeError, KeyError):
            backup_warning("предыдущий журнал нельзя использовать как свидетельство", "previous-journal")
    extras = {".env", "config/local.json", ".stolas/state/resources.json", ".stolas/state/handover.json", ".stolas/state/draft.json", ".stolas/state/progress.json", ".stolas/integrations/n8n/local.json", ".stolas/integrations/n8n/settings.json", ".stolas/integrations/n8n/install-state.json"}
    for name in sorted(set(files) | old_names | extras):
        path = layout.bounded(target, name)
        state["files"][name] = path.is_file()
        if path.is_file():
            state["modes"][name] = path.stat().st_mode & 0o777
            destination = backup / "files" / name
            layout.directory(destination.parent)
            shutil.copy2(path, destination)
            layout.keep_owner(destination)
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
        atomic_json(journal, {"phase": "installed", "backup": backup.relative_to(target).as_posix(), "configured": state["configured"]})
    except BaseException:
        restore(target, backup)
        mark_backup(backup, "rolled_back")
        atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix()})
        raise
    return backup


def restore(target, backup, preserve_manager=False):
    state = read_json(layout.bounded(target, backup.relative_to(target) / "state.json"))
    # An explicit Core rollback must retain PATH ownership/cleanup support even
    # when the preceding release predates that support. State is not versioned.
    keep = {}
    if preserve_manager and read_json(layout.bounded(target, ".stolas/state/entrypoint.json")).get("path"):
        current = read_json(layout.bounded(target, ".stolas/state/managed.json"))
        for name in current.get("files", []):
            if name == "stolas" or name.startswith(".stolas/installer/"):
                path = layout.bounded(target, name)
                if not path.is_file():
                    raise RuntimeError("Команда управления повреждена; повторите обновление перед откатом")
                keep[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    from tools import resources
    images = resources.load(target, "resources.json").get("images", {}) if keep else {}
    for name, existed in state["files"].items():
        if name in keep:
            continue
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
    previous = state["previous_manifest"]
    if keep:
        # Retain ownership of images built by newer Core versions as well.
        restored = resources.load(target, "resources.json")
        resources.save(target, "resources.json", {**restored, "images": {**images, **restored.get("images", {})}})
        previous = {**previous, "files": sorted(set(previous.get("files", [])) | set(keep)),
                    "hashes": {**previous.get("hashes", {}), **keep}, "management_version": layout.VERSION}
    atomic_json(target / ".stolas/state/managed.json", previous)
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


def run_installer(target, configure_only=False, reuse=False, recover=False, skip_first_test=False):
    args = [sys.executable, str(layout.code_root(target) / "tools/install.py"), "--root", str(target)]
    if configure_only:
        args.append("--configure-only")
    if reuse:
        args.append("--reuse-config")
    if recover:
        args.append("--recover")
    if skip_first_test:
        args.append("--skip-first-test")
    return subprocess.run(args, cwd=target).returncode


def rollback_files(target, backup, configure_only=False, preserve_manager=False):
    from tools import resources
    state = read_json(backup / "state.json")
    docker = resources.stop_replacement(target) if state.get("configured") and not configure_only else None
    restore(target, backup, preserve_manager=preserve_manager)
    if docker and not resources.load(target, "resources.json").get("uninstalled"):
        resources.restart(target, docker)
    mark_backup(backup, "rolled_back")


def update_result(target, code):
    from tools.terminal import ui
    terminal = ui()
    if code in (0, 2):
        journal = read_json(layout.bounded(target, ".stolas/state/transaction.json"))
        status = read_json(layout.bounded(target, ".stolas/state/status.json"))
        expected_stage = "complete" if code == 0 else "needs_attention"
        if journal.get("phase") != "complete" or status.get("exit_code") != code or status.get("stage") != expected_stage:
            raise RuntimeError("Обновление не завершено: изменения не зафиксированы")
        version = layout.installed_version(target)
        if version:
            terminal.result("Stolas успешно обновлён до " + version)
        else:
            terminal.result("Обновление завершено; установленную версию определить не удалось.", "warning")
            terminal.line("Проверьте локальные сведения об установленной версии: stolas version")
    elif code == 3:
        terminal.result("Обновление отменено.", "warning")
    elif code == 130:
        terminal.result("Обновление прервано.", "warning")
    else:
        terminal.result("Обновление не выполнено. Причина указана выше.", "error")


def deploy(source, target, ref="checkout", digest="", action=None, configure_only=False):
    target = safe_path(target)
    pending_path = layout.bounded(target, ".stolas/state/transaction.json")
    pending = read_json(pending_path)
    if target.exists() and any(target.iterdir()) and not installed(target) and not recoverable(target) and not pending.get("backup"):
        raise ValueError("В каталоге посторонние файлы. Выберите другой --dir; файлы не изменены")
    if not configure_only and action != "rollback" and (target / ".env").is_file() and (target / "config/local.json").is_file():
        from tools.install import require_docker_access
        layout.preflight(target)
        require_docker_access(target)
    with install_lock(target), backup_reporting(target):
        diagnostic = target / ".stolas/state/status.json"
        journal = target / ".stolas/state/transaction.json"
        pending = read_json(journal)
        # Persist verifiable historical evidence before replacing the journal.
        try:
            records, protected, phase = inspect_backups(target)
            BACKUP_REPORT.get()["checked"] = bool(records)
            reconcile_legacy_backups(target, records, protected, phase)
        except (OSError, ValueError, TypeError):
            backup_warning("исторический журнал или защищённая копия повреждены", "migration")
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
                    raise RuntimeError("Нет точки восстановления для отката")
                backup = layout.bounded(target, previous["backup"])
                if not read_json(backup / "state.json").get("configured"):
                    raise RuntimeError("Точка восстановления не содержит предыдущую установленную версию")
                rollback_files(target, backup, configure_only, preserve_manager=True)
                atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix()})
                from tools.terminal import ui
                ui().result("Предыдущая версия восстановлена; история сохранена")
                return 0
            if source != target or configured and layout.runtime(target):
                if source == target:
                    current = read_json(target / ".stolas/state/managed.json")
                    ref, digest = current.get("ref", ref), current.get("archive_sha256", digest)
                backup = stage_sources(source, target, ref, digest)
            installer_options = {"skip_first_test": True} if configured and action == "reconfigure" else {}
            code = run_installer(target, configure_only, reuse=configured and action == "update", **installer_options)
            if code in (1, 3) and backup and (configured or code == 3):
                rollback_files(target, backup, configure_only)
                atomic_json(diagnostic, {"stage": "rolled_back" if code == 1 else "cancelled", "exit_code": code})
            else:
                if backup and code in (0, 2):
                    finalize(target, backup)
                atomic_json(diagnostic, {"stage": "complete" if code == 0 else "interrupted" if code == 130 else "needs_attention", "exit_code": code, "ref": ref})
            if backup:
                atomic_json(journal, {"phase": "interrupted" if code == 130 or code == 1 and not configured else "rolled_back" if code in (1, 3) else "complete", "backup": backup.relative_to(target).as_posix(), "configured": configured})
                if action == "update" and not configure_only:
                    update_result(target, code)
                if code in (0, 2):
                    try:
                        mark_backup(backup, "complete")
                    except (OSError, ValueError, TypeError):
                        backup_warning("не удалось записать подтверждённый статус", backup)
                    retain_transaction_backups(target)
            elif action == "update" and not configure_only:
                update_result(target, code)
            return code
        except BaseException:
            if backup:
                rollback_files(target, backup, configure_only, preserve_manager=action == "rollback")
                atomic_json(journal, {"phase": "rolled_back", "backup": backup.relative_to(target).as_posix(), "configured": configured})
            if action == "update" and not configure_only:
                from tools.terminal import ui
                ui().result("Обновление не выполнено.", "error")
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
        if isinstance(error, OSError):
            from tools.diagnostics import report
            report(args.target, error)
        else:
            ui().result(str(error) if not isinstance(error, (KeyboardInterrupt, EOFError)) else "Ввод прерван. Повторите команду для продолжения", "error")
        return 130 if isinstance(error, (KeyboardInterrupt, EOFError)) else 1


if __name__ == "__main__":
    sys.exit(main())
