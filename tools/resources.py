"""Exact Docker ownership and reversible service handover; never prune by name."""
import json
import re
import subprocess
import uuid
from pathlib import Path

from tools import environment, layout

CONTAINER_FORMAT = '''{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},"image_id":{{json .Image}},"running":{{json .State.Running}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}'''
VOLUME_FORMAT = '''{"name":{{json .Name}},"labels":{{json .Labels}},"created":{{json .CreatedAt}}}'''
IMAGE_FORMAT = '''{"id":{{json .Id}},"labels":{{json .Config.Labels}}}'''


def read_env(root):
    path = layout.bounded(root, ".env")
    result = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.startswith("STOLAS_"):
                result[key] = value.strip().strip("'\"")
    return result


def command(root, docker=None):
    if docker:
        return docker
    facts = environment.discover(root)
    if not facts["docker"]["available"]:
        raise RuntimeError(facts["docker"]["reason"] + ". Восстановите доступ и повторите команду")
    return facts["docker"]["command"]


def call(root, docker, *args, optional=False):
    result = environment.command(docker + list(args), **({"timeout": 45} if args and args[0] == "stop" else {}))
    if result.returncode and not optional:
        raise RuntimeError("Docker не выполнил действие. Проверьте доступ: docker info")
    return result


def containers(root, docker):
    ids = call(root, docker, "ps", "-aq").stdout.split()
    if not ids:
        return []
    result = call(root, docker, "inspect", "--format", CONTAINER_FORMAT, *ids)
    items = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    # Docker emits mount arrays in map order, which can differ between reads.
    for item in items:
        item["mounts"] = sorted(item.get("mounts", []), key=lambda mount: json.dumps(mount, sort_keys=True))
    return sorted(items, key=lambda item: item["id"])


def owned(item, root, instance=None):
    labels = item.get("labels") or {}
    directory = labels.get("com.docker.compose.project.working_dir")
    if not directory or Path(directory).absolute() != Path(root).absolute() or labels.get("com.docker.compose.service") != "stolas":
        return False
    actual = labels.get("org.stolas.instance")
    return not instance or not actual or actual == instance


def compose(root, docker):
    return docker + ["compose", "--project-directory", str(root), "--env-file", str(root / ".env"), "-f", str(root / "compose.yaml")]


def volume_info(root, docker, name):
    result = call(root, docker, "volume", "inspect", "--format", VOLUME_FORMAT, name, optional=True)
    if result.returncode:
        # Distinguish a missing volume from a lost daemon connection.
        call(root, docker, "info", "--format", "{{.OSType}}")
        return None
    return json.loads(result.stdout)


def image_info(root, docker, name):
    result = call(root, docker, "image", "inspect", "--format", IMAGE_FORMAT, name, optional=True)
    return json.loads(result.stdout) if result.returncode == 0 else None


def core_version(root):
    """Core may be older than the retained manager after an explicit rollback."""
    path = layout.bounded(root, "compose.yaml")
    if path.is_file():
        match = re.search(r"^\s+image:\s*[^\n]+:(\d+\.\d+\.\d+)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return match.group(1)
    return layout.VERSION


def save(root, name, value):
    from tools.deploy import atomic_json
    atomic_json(layout.bounded(root, ".stolas/state/" + name), value)


def load(root, name):
    path = layout.bounded(root, ".stolas/state/" + name)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def configure(root, docker):
    from tools.install import ask, write_private
    from tools.terminal import ui
    env = read_env(root)
    items = containers(root, docker)
    instance = env.get("STOLAS_INSTANCE_ID") or load(root, "identity.json").get("instance") or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", instance):
        raise ValueError("Не читается идентификатор установки. Восстановите .env из резервной копии")
    save(root, "identity.json", {"instance": instance})
    ours = [item for item in items if owned(item, root, env.get("STOLAS_INSTANCE_ID"))]
    old_project = env.get("STOLAS_PROJECT_NAME")
    if not old_project and ours:
        projects = {c["labels"]["com.docker.compose.project"] for c in ours}
        if len(projects) != 1:
            raise RuntimeError("Для каталога найдено несколько проектов Stolas. Уточните STOLAS_PROJECT_NAME в .env")
        old_project = projects.pop()
    project = old_project if old_project and not re.fullmatch(r"stolas-[a-f0-9]{10}", old_project) else "stolas"

    def valid_name(value):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value):
            raise ValueError("Введите 1–63 латинских строчных букв, цифр, _ или -")
        return value

    while True:
        valid_name(project)
        conflicts = [c for c in items if (c["name"].lstrip("/") == project or (c.get("labels") or {}).get("com.docker.compose.project") == project) and c not in ours]
        candidate_image = image_info(root, docker, project + ":" + core_version(root))
        foreign_image = candidate_image and (candidate_image.get("labels") or {}).get("org.stolas.instance") != instance
        candidate_volume = volume_info(root, docker, project + "_stolas-data") if not old_project and not env.get("STOLAS_DATA_VOLUME") else None
        foreign_volume = candidate_volume and (candidate_volume.get("labels") or {}).get("org.stolas.instance") != instance
        if not conflicts and not foreign_image and not foreign_volume:
            break
        ui().result("Имя «" + project + "» занято. Выберите имя этого экземпляра.", "warning")
        project = ask("Имя экземпляра", "stolas-home", valid_name)

    volume = env.get("STOLAS_DATA_VOLUME")
    found = {m["Name"] for c in ours for m in c.get("mounts", []) if m.get("Type") == "volume" and m.get("Destination") == "/data"}
    if len(found) > 1 or volume and found and found != {volume}:
        raise RuntimeError("Обнаружены разные volumes истории. Проверьте подключение /data; данные не изменены")
    if found:
        volume = found.pop()
    if not volume and old_project:
        transaction = load(root, "transaction.json")
        backup = transaction.get("backup")
        previous_compose = layout.bounded(root, backup + "/files/compose.yaml") if backup else root / "compose.yaml"
        if previous_compose.is_file():
            result = call(root, docker, "compose", "--project-directory", str(root), "--env-file", str(root / ".env"), "-f", str(previous_compose), "config", "--format", "json")
            spec = json.loads(result.stdout)
            volume = spec.get("volumes", {}).get("stolas-data", {}).get("name")
    volume = volume or project + "_stolas-data"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}", volume):
        raise ValueError("Недопустимое имя volume истории")
    info = volume_info(root, docker, volume)
    recorded = load(root, "resources.json")
    if info:
        labels = info.get("labels") or {}
        legacy_owned = old_project and labels.get("com.docker.compose.project") == old_project and labels.get("com.docker.compose.volume") == "stolas-data"
        modern_owned = labels.get("org.stolas.instance") == instance and labels.get("org.stolas.directory") == str(root)
        remembered = recorded.get("volume") == info and recorded.get("instance") == instance
        if not (legacy_owned or modern_owned or remembered):
            raise RuntimeError("Volume истории принадлежит другой установке. Выберите другое имя экземпляра; данные не изменены")
    else:
        if old_project and (root / "config/local.json").exists() and not recorded.get("uninstalled"):
            raise RuntimeError("Volume прежней истории не найден. Восстановите его перед обновлением; пустая история не создавалась")
        call(root, docker, "volume", "create", "--label", "org.stolas.instance=" + instance, "--label", "org.stolas.directory=" + str(root), volume)
        info = volume_info(root, docker, volume)
        if info is None:
            raise RuntimeError("Не удалось создать volume истории")
    images = recorded.get("images", {})
    for item in ours:
        images[item["image"]] = item["image_id"]
    save(root, "resources.json", {"instance": instance, "project": project, "volume": info, "images": images})
    handover = load(root, "handover.json")
    if handover.get("phase") not in ("stopping", "started"):
        save(root, "handover.json", {"phase": "planned", "old": ours, "instance": instance, "project": project})
    updates = {"STOLAS_PROJECT_NAME": project, "STOLAS_CONTAINER_NAME": project, "STOLAS_INSTANCE_ID": instance, "STOLAS_DATA_VOLUME": volume}
    text = (root / ".env").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.partition("=")[0] not in updates]
    write_private(root / ".env", "\n".join(lines) + "\n" + "".join(k + "=" + v + "\n" for k, v in updates.items()))


def activate(root, docker):
    handover = load(root, "handover.json")
    save(root, "handover.json", {**handover, "phase": "stopping"})
    items = containers(root, docker)
    previous_ids = {item["id"] for item in handover.get("old", [])}
    for item in items:
        if item["id"] in previous_ids and owned(item, root):
            call(root, docker, "stop", "--time", "35", item["id"])
            call(root, docker, "rm", item["id"])
    save(root, "handover.json", {**handover, "phase": "started"})


def complete(root, docker):
    state = load(root, "resources.json")
    ref = state["project"] + ":" + core_version(root)
    image = image_info(root, docker, ref)
    if image:
        state.setdefault("images", {})[ref] = image["id"]
    state.pop("uninstalled", None)
    save(root, "resources.json", state)
    handover = load(root, "handover.json")
    save(root, "handover.json", {**handover, "phase": "complete"})


def stop_replacement(root):
    handover = load(root, "handover.json")
    if handover.get("phase") not in ("stopping", "started", "complete"):
        return None
    docker = command(root)
    old_ids = {c["id"] for c in handover.get("old", [])}
    for item in containers(root, docker):
        labels = item.get("labels") or {}
        if item["id"] not in old_ids and owned(item, root, handover["instance"]) and labels.get("org.stolas.instance") == handover["instance"]:
            call(root, docker, "stop", "--time", "35", item["id"])
            call(root, docker, "rm", item["id"])
    return docker


def restart(root, docker):
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("STOLAS_", "COMPOSE_"))}
    try:
        result = subprocess.run(compose(root, docker) + ["up", "-d", "--wait", "--wait-timeout", "90", "stolas"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Файлы восстановлены, но сервис не ответил. Проверьте docker info и повторите откат") from None
    if result.returncode:
        from tools.diagnostics import command
        raise RuntimeError("Файлы восстановлены, но прежний сервис не запустился. Повторите " + command(root, "rollback") + " после проверки docker info")
