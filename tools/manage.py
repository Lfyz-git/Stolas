"""Offline instance management; network access is needed only for update."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import diagnostics, entrypoints, environment, layout, resources
from tools.deploy import atomic_json, install_lock, legacy_files, prune_empty, read_json, replace_file, runtime_files, safe_path
from tools.install import Back, Cancel, Rescan, ask, choice, clean_env, first_test, LABELS
from tools.terminal import ui


def removal_plan(root, docker):
    env = resources.read_env(root)
    state = resources.load(root, "resources.json")
    items = resources.containers(root, docker)
    ours = [c for c in items if resources.owned(c, root, env.get("STOLAS_INSTANCE_ID"))]
    project = state.get("project") or env.get("STOLAS_PROJECT_NAME")
    if not project and ours:
        projects = {c["labels"]["com.docker.compose.project"] for c in ours}
        if len(projects) != 1:
            raise RuntimeError("Для каталога найдено несколько проектов. Уточните STOLAS_PROJECT_NAME в .env")
        project = projects.pop()
    volume = state.get("volume")
    mounts = {m["Name"] for c in ours for m in c.get("mounts", []) if m.get("Type") == "volume" and m.get("Destination") == "/data"}
    if len(mounts) > 1:
        raise RuntimeError("Установка использует несколько volumes истории. Проверьте подключение /data")
    name = env.get("STOLAS_DATA_VOLUME") or (next(iter(mounts)) if mounts else None)
    if not name and (root / "compose.yaml").is_file() and (root / ".env").is_file():
        result = environment.command(resources.compose(root, docker) + ["config", "--format", "json"])
        if result.returncode:
            raise RuntimeError("Не удалось прочитать Compose. Исправьте compose.yaml и повторите удаление")
        name = json.loads(result.stdout).get("volumes", {}).get("stolas-data", {}).get("name")
    if name:
        actual = resources.volume_info(root, docker, name)
        if actual:
            labels = actual.get("labels") or {}
            modern = labels.get("org.stolas.instance") == env.get("STOLAS_INSTANCE_ID") and labels.get("org.stolas.directory") == str(root)
            legacy = project and labels.get("com.docker.compose.project") == project and labels.get("com.docker.compose.volume") == "stolas-data"
            if volume and actual != volume or not (volume == actual or modern or legacy):
                raise RuntimeError("Принадлежность volume истории не подтверждена. Данные не изменены")
            volume = actual
        else:
            volume = None
    images = dict(state.get("images", {}))
    for item in ours:
        images[item["image"]] = item["image_id"]
    return {"project": project, "containers": ours, "images": images, "volume": volume, "instance": env.get("STOLAS_INSTANCE_ID") or state.get("instance")}


def managed_paths(root):
    manifest = read_json(layout.bounded(root, ".stolas/state/managed.json"))
    names = set(manifest.get("files", []))
    if not layout.runtime(root):
        names |= legacy_files(root)
    names |= {".env", "config/local.json", ".stolas-install.lock"}
    for name in ("managed.json", "transaction.json", "resources.json", "handover.json", "identity.json", "draft.json", "progress.json", "status.json", "install.lock", "entrypoint.json"):
        names.add(".stolas/state/" + name)
    for name in ("local.json", "settings.json", "install-state.json"):
        names.add(".stolas/integrations/n8n/" + name)
    backups = layout.bounded(root, ".stolas/backups/transactions")
    if backups.exists():
        for state_path in backups.glob("*/state.json"):
            layout.bounded(root, state_path.relative_to(root))
            state = read_json(state_path)
            names.add(state_path.relative_to(root).as_posix())
            for name, existed in state.get("files", {}).items():
                if existed:
                    path = layout.bounded(state_path.parent / "files", name)
                    names.add(path.relative_to(root).as_posix())
    settings = layout.bounded(root, ".stolas/backups/settings")
    if settings.exists():
        for directory in settings.iterdir():
            if re.fullmatch(r"\d{8}T\d{6}-[a-f0-9]{6}", directory.name):
                for name in (".env", "local.json"):
                    names.add((directory / name).relative_to(root).as_posix())
    for name in names:
        layout.bounded(root, name)
    return names


def preserve_legacy_manager(root):
    """Leave a native offline command after removing a legacy Docker service."""
    if layout.runtime(root):
        return set()
    legacy = legacy_files(root)
    files = {name: path for name, path in runtime_files(ROOT).items()
             if name == "stolas" or name.startswith(".stolas/installer/")}
    if "stolas" not in files:
        raise RuntimeError("Не найдена команда управления. Запустите удаление из полного архива v0.5.1")
    previous = read_json(root / ".stolas/state/managed.json")
    for name in files:
        path = layout.bounded(root, name)
        if path.exists() and name not in previous.get("files", []):
            raise RuntimeError("Файл занят: " + name + ". Переместите его и повторите удаление")
    # Record exact ownership before copying, so an interrupted copy is recoverable.
    atomic_json(root / ".stolas/state/managed.json", {"files": sorted(legacy | set(files)), "ref": "v0.4.0"})
    for name, source in files.items():
        replace_file(source, layout.bounded(root, name))
    os.chmod(root / "stolas", 0o755)
    known = read_json(ROOT / "tools/legacy-v0.4.0.json")
    return legacy & (set(known) - {"compose.yaml"})


def uninstall(root, purge=False):
    root = safe_path(root)
    with install_lock(root):
        docker = resources.command(root)
        plan = removal_plan(root, docker)
        names = managed_paths(root)
        terminal = ui()
        terminal.stage("Удаление")
        terminal.line("Найдена установка: " + str(root))
        terminal.line("Проект: " + (plan["project"] or "не создан"))
        terminal.line("Контейнеры: " + (", ".join(c["name"].lstrip("/") for c in plan["containers"]) or "уже удалены"))
        terminal.line("Образы: " + (", ".join(plan["images"]) or "не найдены"))
        terminal.line("История: " + (plan["volume"]["name"] if plan["volume"] else "volume не найден"))
        LABELS.update(remove="Программу, сохранив историю", purge="Полностью, включая историю и настройки", confirm="Удалить программу")
        selected = "purge" if purge else choice("Что удалить?", ("remove", "purge", "cancel"), "remove")
        if selected == "cancel":
            terminal.result("удаление отменено")
            return 0
        purge = selected == "purge"
        if purge:
            terminal.line()
            terminal.result("История и настройки будут удалены без возможности восстановления.", "warning")
            terminal.line("Каталог: " + str(root))
            terminal.line("Volume: " + (plan["volume"]["name"] if plan["volume"] else "отсутствует"))
            if ask("Для подтверждения введите УДАЛИТЬ") != "УДАЛИТЬ":
                terminal.result("удаление отменено")
                return 0
        else:
            terminal.line("История, настройки, резервные копии и команда управления сохранятся.")
            if choice("Подтвердить удаление программы?", ("confirm", "cancel"), "cancel") != "confirm":
                terminal.result("удаление отменено")
                return 0
        # Re-read ownership after the user has reviewed the exact resource list.
        current = removal_plan(root, docker)
        if current != plan:
            raise RuntimeError("Ресурсы изменились во время подтверждения. Повторите удаление")
        entrypoints.check_removal(root)
        retired = preserve_legacy_manager(root) if not purge else set()
        resources.save(root, "resources.json", {"project": plan["project"], "instance": plan["instance"], "volume": plan["volume"], "images": plan["images"], "uninstalled": True})
        for item in plan["containers"]:
            resources.call(root, docker, "stop", "--time", "35", item["id"])
            resources.call(root, docker, "rm", item["id"])
        if purge and plan["volume"]:
            volume = plan["volume"]
            if resources.volume_info(root, docker, volume["name"]) != volume:
                raise RuntimeError("Volume изменился. Повторите удаление после проверки истории")
            users = resources.call(root, docker, "ps", "-aq", "--filter", "volume=" + volume["name"]).stdout.split()
            if users:
                raise RuntimeError("Volume истории используется другим контейнером. Он сохранён; отключите этот контейнер и повторите удаление")
            resources.call(root, docker, "volume", "rm", volume["name"])
        remaining = resources.containers(root, docker)
        for ref, identifier in plan["images"].items():
            image = resources.image_info(root, docker, ref)
            if not image or image["id"] != identifier:
                continue
            if any(c["image_id"] == identifier for c in remaining):
                terminal.result("Общий образ сохранён: " + ref, "warning")
                continue
            resources.call(root, docker, "image", "rm", ref)
        entrypoints.remove(root)
        remove = names if purge else {name for name in names if name.startswith(".stolas/build/")} | retired
        for name in sorted(remove):
            path = layout.bounded(root, name)
            if path.is_file():
                path.unlink()
        prune_empty(root, remove)
        terminal.result("Stolas удалён полностью" if purge else "Агент удалён. История сохранена; восстановление: ./stolas update")
        if purge:
            remaining_files = [p for p in root.rglob("*") if p.is_file() or p.is_symlink()]
            if remaining_files:
                terminal.result("Посторонние файлы сохранены в " + str(root), "warning")
            else:
                try:
                    root.rmdir()
                except OSError:
                    pass
        return 0


def update(root, version=None):
    terminal = ui()
    terminal.stage("Обновление")
    if version is None:
        req = urllib.request.Request("https://api.github.com/repos/Lfyz-git/Stolas/releases/latest", headers={"Accept": "application/vnd.github+json", "User-Agent": "Stolas/" + layout.VERSION})
        with urllib.request.urlopen(req, timeout=20) as response:
            version = json.load(response)["tag_name"]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError("Версия должна иметь вид v0.5.1")
    terminal.line("Версия: " + version)
    with tempfile.TemporaryDirectory(prefix="stolas-update-") as directory:
        path = Path(directory) / "bootstrap.sh"
        with urllib.request.urlopen("https://raw.githubusercontent.com/Lfyz-git/Stolas/" + version + "/bootstrap.sh", timeout=20) as response:
            path.write_bytes(response.read(1024 * 1024))
        return subprocess.run(["sh", str(path), "--dir", str(root), "--ref", version, "--action", "update"], env=clean_env()).returncode


def main():
    parser = argparse.ArgumentParser(description="Stolas — измерения и управление установкой")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="action")
    run = sub.add_parser("run", help="выполнить одно измерение")
    run.add_argument("--json", action="store_true", help="JSON в stdout, события в stderr")
    sub.add_parser("history", help="получить историю в JSON")
    sub.add_parser("logs", help="смотреть логи")
    sub.add_parser("configure", help="изменить настройки")
    upgrade = sub.add_parser("update", help="обновить с сохранением истории")
    upgrade.add_argument("--version", help="версия релиза, например v0.5.1")
    sub.add_parser("rollback", help="вернуть предыдущую версию")
    diagnose = sub.add_parser("diagnose", help="проверить окружение")
    diagnose.add_argument("--json", action="store_true", help="JSON без секретов")
    integration = sub.add_parser("integrate", help="подключить необязательную интеграцию")
    integration.add_argument("client", choices=("n8n",))
    integration.add_argument("--mode", choices=("export", "api"))
    removal = sub.add_parser("uninstall", help="удалить приложение")
    removal.add_argument("--purge", action="store_true", help="включая историю и настройки; требуется подтверждение")
    command = sub.add_parser("command", help="настроить необязательную команду в PATH")
    command.add_argument("operation", choices=("install", "remove", "status"))
    command.add_argument("--scope", choices=("user", "system"), help="пользовательский или общий каталог")
    command.add_argument("--name", help="имя команды для этого экземпляра")
    sub.add_parser("help", help="показать команды")
    args = parser.parse_args()
    try:
        root = safe_path(args.root)
        if args.action in (None, "help"):
            parser.print_help()
            return 0
        if args.action == "command":
            if args.operation == "status":
                record = entrypoints.read(root)
                ui().line(record.get("path", "Команда в PATH не настроена"))
                if record.get("path"):
                    ui().result("Принадлежность подтверждена" if entrypoints.owned(root, record) else "Команда изменена или отсутствует", "success" if entrypoints.owned(root, record) else "warning")
            else:
                with install_lock(root):
                    if args.operation == "remove":
                        entrypoints.remove(root)
                    else:
                        entrypoints.configure(root, args.scope, args.name)
            return 0
        if args.action == "uninstall":
            return uninstall(root, args.purge)
        if args.action == "update":
            layout.preflight(root)
            return update(root, args.version)
        if args.action in ("configure", "rollback"):
            from tools.deploy import deploy
            return deploy(root, root, action="reconfigure" if args.action == "configure" else "rollback")
        if args.action == "integrate":
            from tools.integrate import integrate
            return integrate(root, args.mode)
        if args.action == "diagnose":
            facts = environment.discover(root)
            if args.json:
                print(json.dumps(facts, ensure_ascii=False, indent=2))
            else:
                ui().stage("Диагностика")
                ui().line("Каталог: " + str(root))
                ui().line("Версия: " + facts["installation"].get("ref", "не определена"))
                ui().result("Docker доступен" if facts["docker"]["available"] else facts["docker"]["reason"], "success" if facts["docker"]["available"] else "warning")
                if facts["docker"].get("access_warning"):
                    ui().result(facts["docker"]["access_warning"], "warning")
                ui().line("Настройки: " + ("сохранены" if facts["installation"]["config"] else "не завершены"))
                diagnostics.permissions(facts)
            return 0
        docker = resources.command(root)
        compose = resources.compose(root, docker)
        if args.action == "run" and not args.json:
            data = first_test(compose, root)
            return 0 if data["status"] == "ok" else 2
        command = compose + (["logs", "--tail", "50", "-f", "stolas"] if args.action == "logs" else ["exec", "-T", "stolas", "python3", "-m", "agent", "run" if args.action == "run" else "history"])
        return subprocess.run(command, cwd=root, env=clean_env()).returncode
    except (Back, Cancel, Rescan):
        ui().result("Действие отменено")
        return 3
    except (KeyboardInterrupt, EOFError):
        ui().result("Ввод прерван. Повторите команду для продолжения", "warning")
        return 130
    except (ValueError, RuntimeError) as error:
        ui().result(str(error), "error")
        return 1
    except (OSError, KeyError, subprocess.SubprocessError) as error:
        diagnostics.report(args.root, error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
